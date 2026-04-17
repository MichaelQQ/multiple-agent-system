from __future__ import annotations

from pydantic import ValidationError as PydanticValidationError

__all__ = ["ConfigValidationError"]


class ConfigValidationError(Exception):
    def __init__(self, message: str, errors: list[dict] | None = None):
        super().__init__(message)
        self.errors = errors or []

    @classmethod
    def from_pydantic(cls, exc: PydanticValidationError) -> ConfigValidationError:
        error_messages = []
        for err in exc.errors():
            loc = " -> ".join(str(l) for l in err["loc"])
            msg = err["msg"]
            input_val = err.get("input")
            if input_val is not None and len(str(input_val)) > 50:
                input_repr = f"{str(input_val)[:47]}..."
            else:
                input_repr = str(input_val)
            error_messages.append({
                "field": loc,
                "message": msg,
                "input": input_repr,
            })
        lines = ["Configuration validation failed:"]
        for e in error_messages:
            lines.append(f"  - Field '{e['field']}': {e['message']}")
            lines.append(f"    Received: {e['input']}")
        return cls(message="\n".join(lines), errors=error_messages)

    def to_user_friendly(self) -> str:
        if not self.errors:
            return str(self)
        lines = ["Configuration validation failed. Please fix the following errors:"]
        for i, e in enumerate(self.errors, 1):
            lines.append(f"\n{i}. Field '{e['field']}'")
            lines.append(f"   Problem: {e['message']}")
            lines.append(f"   Received: {e['input']}")
            lines.append(f"   Hint: Check the field definition in your config.yaml or roles.yaml")
        return "\n".join(lines)
