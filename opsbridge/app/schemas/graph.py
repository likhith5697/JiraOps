"""Inbound Microsoft Graph change / lifecycle notification payloads."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ResourceData(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    id: str | None = None
    odata_type: str | None = Field(default=None, alias="@odata.type")


class ChangeNotification(BaseModel):
    model_config = ConfigDict(extra="ignore")

    subscriptionId: str = Field(max_length=128)
    clientState: str | None = Field(default=None, max_length=256)
    changeType: str | None = None
    resource: str | None = Field(default=None, max_length=1024)
    resourceData: ResourceData | None = None
    tenantId: str | None = None
    subscriptionExpirationDateTime: str | None = None
    lifecycleEvent: str | None = None


class ChangeNotificationCollection(BaseModel):
    model_config = ConfigDict(extra="ignore")
    value: list[ChangeNotification] = Field(default_factory=list, max_length=1000)
