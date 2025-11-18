"""
Database Schemas for Flow (Multi-listing Calendar SaaS)

Each Pydantic model corresponds to a MongoDB collection (lowercased class name).
"""
from pydantic import BaseModel, Field, HttpUrl
from typing import Optional
from datetime import datetime

class Listing(BaseModel):
    name: str = Field(..., description="Listing name, e.g., 'Villa Azul'")
    color: Optional[str] = Field(None, description="Hex/UI color for listing chips")

class CalendarSource(BaseModel):
    listing_id: str = Field(..., description="ID of the parent Listing")
    name: str = Field(..., description="Human-friendly name, e.g., 'Airbnb' or 'Booking'")
    url: HttpUrl = Field(..., description="iCal feed URL from OTA (Airbnb/Booking/VRBO/etc.)")
    source_type: str = Field("ical", description="Type of source, default 'ical'")
    color: Optional[str] = Field(None, description="Hex color for UI (overrides listing color)")

class Event(BaseModel):
    listing_id: str = Field(..., description="ID of the Listing")
    source_id: str = Field(..., description="ID of the CalendarSource")
    uid: Optional[str] = Field(None, description="Unique UID from the event if present")
    title: str = Field(..., description="Event title")
    start: datetime = Field(..., description="Start datetime (UTC)")
    end: datetime = Field(..., description="End datetime (UTC)")
    all_day: bool = Field(False, description="True if all-day event")
    location: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    raw_url: Optional[str] = Field(None, description="Source iCal URL for traceability")

class ExportRequest(BaseModel):
    webhook_url: HttpUrl = Field(..., description="Apps Script Web App URL or any webhook to receive events JSON")
    range_days: int = Field(30, ge=1, le=365, description="How many days ahead to export")
    listing_id: Optional[str] = Field(None, description="If provided, limit export to this listing")

class WhatsAppRequest(BaseModel):
    recipient_phone: str = Field(..., description="Recipient phone number in international format, e.g., +14155550100")
    message: Optional[str] = Field(None, description="Optional custom message. If omitted, a schedule summary will be generated.")
    token: Optional[str] = Field(None, description="WhatsApp Cloud API token. If omitted, will use WHATSAPP_TOKEN env var.")
    phone_number_id: Optional[str] = Field(None, description="WhatsApp Cloud API phone number ID. If omitted, will use WHATSAPP_PHONE_NUMBER_ID env var.")
    listing_id: Optional[str] = Field(None, description="If provided, limit schedule to this listing")
