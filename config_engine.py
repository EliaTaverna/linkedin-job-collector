"""Interpret title_filter.yaml, keyword_filter.yaml and llm_output.yaml."""

import re
from pathlib import Path

import yaml


def load_yaml(path):
    path = Path(path)
    with path.open(encoding="utf-8") as file:
        data = yaml.safe_load(file)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain key: value settings.")
    return data


def column_name(number):
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def compile_terms(terms, group):
    if not isinstance(terms, list):
        raise ValueError(f"{group} must be a list of strings.")
    compiled = []
    for term in terms:
        if not isinstance(term, str) or not term.strip():
            raise ValueError(f"{group} contains an empty or invalid term.")
        term = term.strip()
        is_prefix = term.endswith("-")
        base = term[:-1].strip() if is_prefix else term
        if not base:
            raise ValueError("A prefix must contain letters before its trailing '-'.")
        # Match phrases across whitespace or hyphens, including Unicode hyphens.
        pattern = r"[\s\u2010-\u2015-]+".join(re.escape(word) for word in base.split())
        if is_prefix:
            pattern += r"\w*"
        compiled.append((term, re.compile(r"\b" + pattern + r"\b", re.IGNORECASE)))
    return compiled


class TitleFilter:
    """Reject titles containing any configured whole word, phrase or prefix."""

    @classmethod
    def from_yaml(cls, path):
        return cls(load_yaml(path))

    def __init__(self, config):
        if not isinstance(config, dict) or set(config) - {"enabled", "reject_any"}:
            raise ValueError("Invalid title_filter.yaml settings.")
        self.enabled = config.get("enabled", True)
        if type(self.enabled) is not bool:
            raise ValueError("title_filter.enabled must be true or false.")
        self.reject_any = compile_terms(config.get("reject_any", []), "title_filter.reject_any")
        if self.enabled and not self.reject_any:
            raise ValueError("title_filter.reject_any must contain at least one term.")

    def evaluate(self, title):
        if not self.enabled:
            return True, "Title filter disabled."
        # Missing metadata is not proof of an unwanted title.
        if not isinstance(title, str) or not title.strip():
            return True, "No title to filter."
        matches = [term for term, pattern in self.reject_any if pattern.search(title)]
        if matches:
            return False, "Excluded title term(s): " + ", ".join(matches)
        return True, "Title filter passed."

    def matches(self, title):
        return self.evaluate(title)[0]


class KeywordFilter:
    """Require every all_of term AND at least one any_of term."""

    @classmethod
    def from_yaml(cls, path):
        return cls(load_yaml(path))

    def __init__(self, config):
        if not isinstance(config, dict):
            raise ValueError("keyword_filter.yaml must contain the keyword rules.")
        if set(config) - {"enabled", "all_of", "any_of"}:
            raise ValueError("Unknown keyword_filter setting.")
        self.enabled = config.get("enabled", True)
        if type(self.enabled) is not bool:
            raise ValueError("keyword_filter.enabled must be true or false.")
        self.all_of = compile_terms(config.get("all_of", []), "keyword_filter.all_of")
        self.any_of = compile_terms(config.get("any_of", []), "keyword_filter.any_of")
        if self.enabled and not self.any_of:
            raise ValueError("keyword_filter.any_of must contain at least one term.")

    def evaluate(self, description):
        """Return (accepted, reason). Match description text only."""
        if not self.enabled:
            return True, "Keyword filter disabled."
        if not isinstance(description, str) or not description.strip():
            return False, "Description is empty."
        missing = [term for term, pattern in self.all_of if not pattern.search(description)]
        if missing:
            return False, "Missing required all_of term(s): " + ", ".join(missing)
        if not any(pattern.search(description) for _, pattern in self.any_of):
            return False, "No any_of term matched the description."
        return True, "Keyword filter matched."

    def matches(self, description):
        return self.evaluate(description)[0]


class LLMOutput:
    """Read output fields, applicant languages and acceptance rules from YAML."""

    @classmethod
    def from_yaml(cls, path):
        return cls(load_yaml(path))

    def __init__(self, config):
        allowed = {"instructions", "known_languages", "fields", "on_language_rejection"}
        if not isinstance(config, dict) or set(config) - allowed:
            raise ValueError("Invalid llm_output.yaml settings.")
        self.instructions = config.get("instructions")
        if not isinstance(self.instructions, str) or not self.instructions.strip():
            raise ValueError("llm_output.yaml needs nonempty instructions.")
        known = config.get("known_languages", [])
        if not isinstance(known, list) or any(not isinstance(value, str) or not value.strip() for value in known):
            raise ValueError("known_languages must be a list of language names.")
        if known:
            self.instructions += "\nThe applicant's known human languages are: " + ", ".join(known) + "."
        fields = config.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise ValueError("llm_output.yaml needs a nonempty fields mapping.")
        self.fields = list(fields)
        self.enums = {}
        self.filters = {}
        properties = {}
        for name, field in fields.items():
            if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                raise ValueError("Output field names must use lowercase letters, digits and underscores.")
            if not isinstance(field, dict) or set(field) - {"values", "description", "keep", "type"}:
                raise ValueError(f"Invalid definition for output field {name}.")
            if field.get("type", "string") != "string":
                raise ValueError(f"fields.{name}.type must be string.")
            description = field.get("description")
            if not isinstance(description, str) or not description.strip():
                raise ValueError(f"fields.{name}.description is required.")
            properties[name] = {"type": "string", "description": description}
            if "values" in field:
                values = field["values"]
                if not isinstance(values, list) or not values or any(not isinstance(value, str) for value in values):
                    raise ValueError(f"fields.{name}.values must be a nonempty list of quoted strings.")
                if len(set(values)) != len(values):
                    raise ValueError(f"fields.{name}.values contains duplicates.")
                keep = field.get("keep", values)
                if not isinstance(keep, list) or any(value not in values for value in keep):
                    raise ValueError(f"fields.{name}.keep must contain only allowed values.")
                self.enums[name] = list(values)
                self.filters[name] = list(keep)
                properties[name]["enum"] = list(values)
            elif "keep" in field:
                raise ValueError(f"fields.{name}: keep requires a values list.")
        self.schema = {
            "type": "object", "properties": properties,
            "required": self.fields, "additionalProperties": False,
        }
        self.language_rejection = config.get("on_language_rejection", {})
        if not isinstance(self.language_rejection, dict) or any(
            name not in fields or not isinstance(value, str)
            or (name in self.enums and value not in self.enums[name])
            for name, value in self.language_rejection.items()
        ):
            raise ValueError("on_language_rejection must use valid output fields and values.")

    def validate(self, result):
        if not isinstance(result, dict) or set(result) != set(self.fields):
            raise ValueError("The model did not return exactly the configured output fields.")
        if any(not isinstance(result[name], str) for name in self.fields):
            raise ValueError("All configured output fields must contain strings.")
        if any(result[name] not in values for name, values in self.enums.items()):
            raise ValueError("The model returned an invalid classification value.")
        return result

    def blank_result(self, language_rejected=False):
        result = {name: "" for name in self.fields}
        if language_rejected:
            result.update(self.language_rejection)
        return result
