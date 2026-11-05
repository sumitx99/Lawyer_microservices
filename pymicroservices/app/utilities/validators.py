import re
from typing import Annotated

from pydantic import AfterValidator

_EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")


def _email(value: str) -> str:
    value = value.strip().lower()
    if len(value) > 320 or not _EMAIL.match(value):
        raise ValueError("invalid email")
    return value


# Plain syntax check (email-validator rejects reserved test domains such as .test).
Email = Annotated[str, AfterValidator(_email)]
