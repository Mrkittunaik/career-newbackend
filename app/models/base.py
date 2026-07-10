from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from bson import ObjectId
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

# ---------------------------------------------------------------
# ObjectId handling for Pydantic v2.
# Mongo _id values arrive as bson.ObjectId; we accept either an
# ObjectId or a valid ObjectId-string on input, and always store/
# serialize as a plain str on the way out to API/JSON consumers.
# ---------------------------------------------------------------

def _validate_object_id(value: Any) -> str:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, str) and ObjectId.is_valid(value):
        return value
    raise ValueError(f"Invalid ObjectId: {value!r}")


PyObjectId = Annotated[str, BeforeValidator(_validate_object_id)]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MongoBaseModel(BaseModel):
    """Base for every document model. Handles the _id <-> id alias and
    gives Mongo docs (which use ObjectId/datetime) a clean pydantic v2 surface."""

    id: Optional[PyObjectId] = Field(default=None, alias="_id")

    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
        json_encoders={ObjectId: str},
    )

    def to_mongo(self) -> dict:
        """Dict ready for insertion/update, dropping unset id so Mongo can assign it."""
        data = self.model_dump(by_alias=True, exclude_none=True)
        if data.get("_id") is None:
            data.pop("_id", None)
        return data
