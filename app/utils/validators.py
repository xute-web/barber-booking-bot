import re


def normalize_name(value: str) -> str | None:
    """Accept letters with single spaces, hyphens or apostrophes between them."""
    name = re.sub(r" +", " ", value.strip())
    if not 1 <= len(name) <= 80:
        return None
    return name if all(part.isalpha() for part in re.split(r"[ '\-’]", name)) else None


def normalize_phone(value: str) -> str | None:
    """Accept Russian phone numbers and return the +7 format."""
    phone = re.sub(r"[ ()-]", "", value.strip())
    if not re.fullmatch(r"(?:\+7|7|8)[0-9]{10}", phone):
        return None
    return "+7" + phone[-10:]
