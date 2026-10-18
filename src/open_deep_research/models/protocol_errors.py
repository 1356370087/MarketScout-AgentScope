"""Framework-independent model protocol errors shared by both engines."""

import json
import re
from difflib import get_close_matches
from itertools import islice

def schema_repair_feedback(validation_errors):
    """Show each failing field and relevant choices, not a truncated global enum."""
    errors = []
    for error in islice(validation_errors, 6):
        item = {"path": list(error.absolute_path), "constraint": error.validator}
        if error.validator == "enum":
            choices = error.validator_value
            if isinstance(error.instance, str) and all(isinstance(choice, str) for choice in choices):
                prefix, separator, _ = error.instance.rpartition(":")
                related = [choice for choice in choices if separator and choice.startswith(prefix + separator)]
                choices = related or get_close_matches(error.instance, choices, n=6, cutoff=0)
            item.update(invalid_value=error.instance, allowed_choices=choices[:6],
                other_allowed_count=len(error.validator_value) - len(choices[:6]))
        else:
            item["message"] = error.message[:400]
        errors.append(item)
    return json.dumps({"errors": errors}, ensure_ascii=False)[:2000]


def parse_structured_json(value: str):
    """Normalize complete JSON losslessly; conflicting duplicate values fail."""
    value = value.strip()
    wrapped = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*)\r?\n```", value, re.I | re.S)
    if wrapped:
        value = wrapped[1]

    def unique_properties(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                # Comparing JSON representations also distinguishes booleans
                # from numbers, including inside nested lists/objects.
                if json.dumps(result[key], ensure_ascii=False, separators=(",", ":")) != json.dumps(item, ensure_ascii=False, separators=(",", ":")):
                    raise json.JSONDecodeError("conflicting duplicate structured-output property", value, 0)
                continue
            result[key] = item
        return result

    return json.loads(value, object_pairs_hook=unique_properties)


class MessageCodecError(ValueError):
    """Raised when a message cannot be represented by Gateway Wire V2."""


class ModelGatewayError(RuntimeError):
    """Sanitized terminal Gateway failure after LiteLLM has exhausted policy."""

    def __init__(
        self,
        code: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
        provider_error_code: str | None = None,
    ) -> None:
        """Create a stable error without copying upstream response content."""
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.request_id = request_id
        # Whitelisted identifier extracted from the upstream error payload (or
        # the canonical token-limit marker); never free-form provider text.
        self.provider_error_code = provider_error_code
