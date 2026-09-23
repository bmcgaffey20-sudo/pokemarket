import re


class TrackingValidationError(ValueError):
    pass


def classify_tracking_number(value, carrier=None):
    normalized = re.sub(r"[\s-]+", "", (value or "").strip()).upper()
    patterns = {
        "UPS": r"1Z[0-9A-Z]{16}",
        "USPS": r"(?:[A-Z]{2}\d{9}US|\d{20,22})",
        "FedEx": r"(?:\d{12}|\d{15}|\d{20}|\d{22})",
    }
    if carrier is not None:
        if carrier not in patterns or not re.fullmatch(patterns[carrier], normalized):
            raise TrackingValidationError("Tracking number does not match the selected carrier's supported format. Check the number and carrier on your label.")
        return normalized, carrier
    if re.fullmatch(r"1Z[0-9A-Z]{16}", normalized):
        return normalized, "UPS"
    if re.fullmatch(r"(?:[A-Z]{2}\d{9}US|\d{20,22})", normalized):
        return normalized, "USPS"
    if re.fullmatch(r"\d{12}|\d{15}|\d{20}|\d{22}", normalized):
        return normalized, "FedEx"
    raise TrackingValidationError(
        "Enter a valid USPS, UPS, or FedEx tracking number. Carrier status is confirmed separately."
    )
