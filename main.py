import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from database import db, create_document, get_documents
from schemas import CalendarSource, Event, ExportRequest, WhatsAppRequest

app = FastAPI(title="Calendar Aggregator API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SourceOut(CalendarSource):
    id: str


def parse_ical(url: str) -> List[dict]:
    """Download and parse an iCal feed URL and return event dicts compatible with Event model."""
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to fetch iCal: {e}")

    text = resp.text
    # Minimal iCal parsing without external heavy deps
    # We'll parse VEVENT blocks manually to avoid timezone pitfalls
    events: List[dict] = []
    lines = [l.strip() for l in text.splitlines()]
    # Handle folded lines
    unfolded = []
    for line in lines:
        if line.startswith(" ") or line.startswith("\t"):
            if unfolded:
                unfolded[-1] += line[1:]
        else:
            unfolded.append(line)

    cur = {}
    in_event = False
    for line in unfolded:
        if line == "BEGIN:VEVENT":
            in_event = True
            cur = {}
            continue
        if line == "END:VEVENT":
            if cur.get("DTSTART") and cur.get("DTEND"):
                try:
                    start_val = cur.get("DTSTART")
                    end_val = cur.get("DTEND")
                    all_day = False
                    def parse_dt(val: str):
                        if val.endswith("Z"):
                            return datetime.strptime(val, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                        if "T" in val:
                            # Treat as naive local -> assume UTC
                            return datetime.strptime(val, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
                        # Date only (all-day)
                        dt = datetime.strptime(val, "%Y%m%d").replace(tzinfo=timezone.utc)
                        return dt
                    if len(start_val) == 8 and "T" not in start_val:
                        all_day = True
                    start = parse_dt(start_val)
                    end = parse_dt(end_val)
                    events.append({
                        "uid": cur.get("UID"),
                        "title": cur.get("SUMMARY") or "(No title)",
                        "start": start,
                        "end": end,
                        "all_day": all_day,
                        "location": cur.get("LOCATION"),
                        "description": cur.get("DESCRIPTION"),
                        "status": cur.get("STATUS"),
                    })
                except Exception:
                    pass
            in_event = False
            cur = {}
            continue
        if in_event:
            if ":" in line:
                key, val = line.split(":", 1)
                key = key.split(";")[0]
                cur[key] = val

    return events


@app.get("/")
def read_root():
    return {"message": "Calendar Aggregator API running"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": "❌ Not Set" if not os.getenv("DATABASE_URL") else "✅ Set",
        "database_name": "❌ Not Set" if not os.getenv("DATABASE_NAME") else "✅ Set",
        "connection_status": "Not Connected",
        "collections": []
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            try:
                response["collections"] = db.list_collection_names()
                response["connection_status"] = "Connected"
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️ Connected but Error: {e}"[:120]
    except Exception as e:
        response["database"] = f"❌ Error: {e}"[:120]
    return response


@app.post("/api/sources", response_model=SourceOut)
def add_source(source: CalendarSource):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    # Prevent duplicates by URL
    existing = db["calendarsource"].find_one({"url": str(source.url)})
    if existing:
        return {
            "id": str(existing["_id"]),
            **source.model_dump(),
        }
    new_id = create_document("calendarsource", source)
    return {"id": new_id, **source.model_dump()}


@app.get("/api/sources", response_model=List[SourceOut])
def list_sources():
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    docs = get_documents("calendarsource")
    out: List[SourceOut] = []
    for d in docs:
        out.append(SourceOut(
            id=str(d.get("_id")),
            name=d.get("name"),
            url=d.get("url"),
            source_type=d.get("source_type", "ical"),
            color=d.get("color")
        ))
    return out


class SyncResponse(BaseModel):
    sources_synced: int
    events_saved: int


@app.post("/api/sync", response_model=SyncResponse)
def sync_calendars(source_id: Optional[str] = None):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    sources = []
    if source_id:
        doc = db["calendarsource"].find_one({"_id": __import__("bson").ObjectId(source_id)})
        if not doc:
            raise HTTPException(status_code=404, detail="Source not found")
        sources = [doc]
    else:
        sources = list(db["calendarsource"].find({}))

    total_events = 0
    for s in sources:
        url = s.get("url")
        sid = str(s.get("_id"))
        # Clear existing events for this source to avoid duplicates
        db["event"].delete_many({"source_id": sid})
        parsed = parse_ical(url)
        batch = []
        for e in parsed:
            ev = Event(
                source_id=sid,
                uid=e.get("uid"),
                title=e.get("title") or s.get("name"),
                start=e.get("start"),
                end=e.get("end"),
                all_day=bool(e.get("all_day")),
                location=e.get("location"),
                description=e.get("description"),
                status=e.get("status"),
                raw_url=url,
            ).model_dump()
            ev["created_at"] = datetime.now(timezone.utc)
            ev["updated_at"] = datetime.now(timezone.utc)
            batch.append(ev)
        if batch:
            db["event"].insert_many(batch)
            total_events += len(batch)

    return SyncResponse(sources_synced=len(sources), events_saved=total_events)


class EventsOut(BaseModel):
    events: List[dict]


@app.get("/api/events", response_model=EventsOut)
def get_events(start: Optional[str] = Query(None), end: Optional[str] = Query(None)):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    q = {}
    if start:
        try:
            start_dt = datetime.fromisoformat(start)
            q.setdefault("start", {})["$gte"] = start_dt
        except Exception:
            pass
    if end:
        try:
            end_dt = datetime.fromisoformat(end)
            q.setdefault("end", {})["$lte"] = end_dt
        except Exception:
            pass

    docs = list(db["event"].find(q))
    # Map source colors/names
    sources = {str(s["_id"]): s for s in db["calendarsource"].find({})}

    def serialize(ev):
        sid = ev.get("source_id")
        src = sources.get(sid, {})
        return {
            "id": str(ev.get("_id")),
            "title": ev.get("title"),
            "start": ev.get("start").isoformat() if isinstance(ev.get("start"), datetime) else ev.get("start"),
            "end": ev.get("end").isoformat() if isinstance(ev.get("end"), datetime) else ev.get("end"),
            "all_day": ev.get("all_day", False),
            "location": ev.get("location"),
            "description": ev.get("description"),
            "status": ev.get("status"),
            "source": {
                "id": sid,
                "name": src.get("name"),
                "color": src.get("color"),
            }
        }

    return {"events": [serialize(e) for e in docs]}


@app.post("/api/export-to-sheet")
def export_to_sheet(payload: ExportRequest):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=payload.range_days)
    docs = list(db["event"].find({"start": {"$lt": end}, "end": {"$gt": now}}).sort("start", 1))
    sources = {str(s["_id"]): s for s in db["calendarsource"].find({})}
    rows = []
    for d in docs:
        sid = d.get("source_id")
        src = sources.get(sid, {})
        rows.append({
            "source": src.get("name"),
            "title": d.get("title"),
            "start": d.get("start").isoformat() if isinstance(d.get("start"), datetime) else d.get("start"),
            "end": d.get("end").isoformat() if isinstance(d.get("end"), datetime) else d.get("end"),
            "all_day": d.get("all_day", False),
            "location": d.get("location"),
            "description": d.get("description"),
            "status": d.get("status"),
        })
    try:
        r = requests.post(str(payload.webhook_url), json={"events": rows}, timeout=20)
        r.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Webhook error: {e}")
    return {"sent": len(rows), "webhook_status": r.status_code}


@app.post("/api/whatsapp/send-schedule")
def whatsapp_send_schedule(payload: WhatsAppRequest):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not configured")

    token = payload.token or os.getenv("WHATSAPP_TOKEN")
    phone_number_id = payload.phone_number_id or os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    if not token or not phone_number_id:
        raise HTTPException(status_code=400, detail="Missing WhatsApp credentials (token/phone_number_id)")

    now = datetime.now(timezone.utc)
    end = now + timedelta(days=7)
    docs = list(db["event"].find({"start": {"$lt": end}, "end": {"$gt": now}}).sort("start", 1))
    sources = {str(s["_id"]): s for s in db["calendarsource"].find({})}

    if payload.message:
        body = payload.message
    else:
        # Build a concise schedule summary
        lines = ["Upcoming schedule (next 7 days):"]
        current_day = None
        for d in docs:
            start: datetime = d.get("start")
            end_dt: datetime = d.get("end")
            if isinstance(start, str):
                try: start = datetime.fromisoformat(start)
                except Exception: pass
            if isinstance(end_dt, str):
                try: end_dt = datetime.fromisoformat(end_dt)
                except Exception: pass
            day = start.strftime("%a %d %b")
            if day != current_day:
                lines.append(f"\n{day}")
                current_day = day
            src = sources.get(d.get("source_id"), {})
            time_part = "All-day" if d.get("all_day") else f"{start.strftime('%H:%M')}–{end_dt.strftime('%H:%M')}"
            lines.append(f"• {time_part} · {d.get('title')} ({src.get('name','')})")
        body = "\n".join(lines) if len(lines) > 1 else "No upcoming events."

    url = f"https://graph.facebook.com/v17.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": payload.recipient_phone,
        "type": "text",
        "text": {"body": body}
    }
    try:
        r = requests.post(url, headers=headers, json=data, timeout=20)
        r.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"WhatsApp API error: {e}")

    return {"status": "sent", "message_length": len(body)}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
