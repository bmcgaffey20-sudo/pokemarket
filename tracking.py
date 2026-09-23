import re


class TrackingValidationError(ValueError):
    pass


def classify_tracking_number(value):
    normalized = re.sub(r"[\s-]+", "", (value or "").strip()).upper()
    if re.fullmatch(r"1Z[0-9A-Z]{16}", normalized):
        return normalized, "UPS"
    if re.fullmatch(r"(?:[A-Z]{2}\d{9}US|\d{20,22})", normalized):
        return normalized, "USPS"
    if re.fullmatch(r"\d{12}|\d{15}|\d{20}|\d{22}", normalized):
        return normalized, "FedEx"
    raise TrackingValidationError(
        "Enter a valid USPS, UPS, or FedEx tracking number. Carrier status is confirmed separately."
    )
