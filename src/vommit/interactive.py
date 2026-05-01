# pragma: exclude file

import typing as t

import questionary
from configuraptor import TypedConfig, load_into

_ConfigT = t.TypeVar("_ConfigT", bound=TypedConfig)

_PROMPT_STYLE = questionary.Style(
    [
        ("qmark", "fg:#f97316 bold"),
        ("question", "fg:#f8fafc bold"),
        ("answer", "fg:#22c55e bold"),
        ("pointer", "fg:#f97316 bold"),
        ("highlighted", "fg:#facc15 bold"),
        ("selected", "fg:#22c55e"),
        ("instruction", "fg:#94a3b8 italic"),
        ("text", "fg:#e2e8f0"),
        ("disabled", "fg:#64748b"),
    ]
)


def _is_typed_config_type(tp: t.Any) -> bool:
    return isinstance(tp, type) and issubclass(tp, TypedConfig)


def _container_kind(tp: t.Any) -> type[dict] | type[list] | None:
    origin = t.get_origin(tp)
    if origin in (dict, list):
        return origin
    if tp in (dict, list):
        return tp
    return None


def _ask_for_value(
    field_name: str,
    label: str,
    field_type: t.Any,
    has_default: bool,
    default: t.Any,
    step: int,
    total_steps: int,
) -> t.Any:
    origin = t.get_origin(field_type)
    if origin is t.Literal:
        return _select_literal(
            field_name, label, field_type, has_default, default, step, total_steps
        )
    elif field_type is bool:
        selected = questionary.confirm(
            message=_prompt_message(
                label, default if has_default else None, step, total_steps
            ),
            default=bool(default) if has_default else False,
            auto_enter=False,
            qmark=">>",
            style=_PROMPT_STYLE,
        ).ask()
        if selected is None:
            raise KeyboardInterrupt(f"Input cancelled for field '{field_name}'.")
        return selected

    raw = questionary.text(
        message=_prompt_message(
            label, default if has_default else None, step, total_steps
        ),
        default=f"{default} " if has_default else "",
        qmark=">>",
        style=_PROMPT_STYLE,
    ).ask()
    if raw is None:
        raise KeyboardInterrupt(f"Input cancelled for field '{field_name}'.")
    raw = (raw or "").strip()

    if raw == "":
        if has_default:
            return default
        raise ValueError(f"Field '{field_name}' is required and cannot be empty.")

    try:
        return _parse_scalar(field_type, raw)
    except ValueError as exc:
        raise ValueError(f"Invalid value for field '{field_name}': {exc}") from exc


def _select_literal(
    field_name: str,
    label: str,
    field_type: t.Any,
    has_default: bool,
    default: t.Any,
    step: int,
    total_steps: int,
) -> t.Any:
    choices = list(t.get_args(field_type))
    if not choices:
        raise ValueError(f"Literal field '{field_name}' has no choices.")

    selected = questionary.select(
        message=_prompt_message(
            label, default if has_default else None, step, total_steps
        ),
        choices=[str(choice) for choice in choices],
        default=str(default) if has_default else None,
        qmark=">>",
        pointer="=>",
        style=_PROMPT_STYLE,
    ).ask()
    if selected is None:
        raise KeyboardInterrupt(f"Selection cancelled for field '{field_name}'.")
    for choice in choices:
        if str(choice) == selected:
            return choice
    raise ValueError(f"Unexpected literal selection for field '{field_name}'.")


def _parse_scalar(field_type: t.Any, raw: str) -> t.Any:
    if field_type is str:
        return raw
    if field_type is int:
        return int(raw)
    if field_type is float:
        return float(raw)
    return raw


def _prompt_message(
    field_name: str,
    default: t.Any = None,
    step: int = 1,
    total_steps: int = 1,
) -> str:
    prefix = f"[{step}/{total_steps}]"
    if default is None:
        return f"{prefix} {field_name}:"
    return f"{prefix} {field_name} [default: {default}]:"


def _print_section_header(cls: type[TypedConfig], depth: int) -> None:
    name = cls.__name__
    indent = "  " * depth
    questionary.print(f"{indent}{name}", style="fg:#64748b")


def _unwrap_annotated(tp: t.Any) -> tuple[t.Any, str | None]:
    if t.get_origin(tp) is not t.Annotated:
        return tp, None

    args = t.get_args(tp)
    if not args:
        return tp, None

    base_type = args[0]
    metadata = args[1:]
    for item in metadata:
        if isinstance(item, str) and item.strip():
            return base_type, item.strip()
        if isinstance(item, dict) and "label" in item and str(item["label"]).strip():
            return base_type, str(item["label"]).strip()
        label_attr = getattr(item, "label", None)
        if isinstance(label_attr, str) and label_attr.strip():
            return base_type, label_attr.strip()
    return base_type, None


def _enabled_field_name(fields: list[tuple[str, t.Any, str]]) -> str | None:
    for field_name, field_type, _ in fields:
        if field_name == "enabled" and field_type is bool:
            return field_name
    return None


def _interactive_build(
    cls: type[_ConfigT],
    depth: int = 0,
    defaults: _ConfigT | None = None,
) -> _ConfigT:
    values: dict[str, t.Any] = {}
    hints = t.get_type_hints(cls, include_extras=True)
    fields: list[tuple[str, t.Any, str]] = []
    for field_name, annotated_type in hints.items():
        field_type, annotated_label = _unwrap_annotated(annotated_type)
        fields.append((field_name, field_type, annotated_label or field_name))

    enabled_field = _enabled_field_name(fields)
    if enabled_field:
        fields = sorted(fields, key=lambda item: item[0] != enabled_field)

    _print_section_header(cls, depth)

    scalar_fields = [
        (field_name, field_type, label)
        for field_name, field_type, label in fields
        if not _container_kind(field_type) and not _is_typed_config_type(field_type)
    ]
    total_scalar_fields = max(1, len(scalar_fields))
    prompt_step = 0

    skip_remaining = False
    for field_name, field_type, label in fields:
        has_default = defaults is not None and hasattr(defaults, field_name)
        default = getattr(defaults, field_name, None) if has_default else None
        if not has_default:
            has_default = hasattr(cls, field_name)
            default = getattr(cls, field_name, None)

        if skip_remaining:
            continue

        container_kind = _container_kind(field_type)
        if container_kind:
            if has_default:
                values[field_name] = default
            else:
                values[field_name] = {} if container_kind is dict else []
            continue

        if _is_typed_config_type(field_type):
            nested_defaults = None
            if defaults is not None and hasattr(defaults, field_name):
                maybe_nested_defaults = getattr(defaults, field_name)
                if isinstance(maybe_nested_defaults, TypedConfig):
                    nested_defaults = maybe_nested_defaults
            values[field_name] = _interactive_build(
                field_type, depth + 1, nested_defaults
            )
            continue

        prompt_step += 1
        values[field_name] = _ask_for_value(
            field_name,
            label,
            field_type,
            has_default,
            default,
            prompt_step,
            total_scalar_fields,
        )
        if (
            enabled_field
            and field_name == enabled_field
            and values[field_name] is False
        ):
            skip_remaining = True

    return t.cast(_ConfigT, load_into(cls, values))


class InteractiveConfig(TypedConfig):
    @classmethod
    def interactive(
        cls: type[_ConfigT],
        defaults: _ConfigT | None = None,
    ) -> _ConfigT:
        return _interactive_build(cls, defaults=defaults)


# class Example(TypedConfig, Interactive):
#     name: str
#     age: t.Annotated[int, "How old are you?"] = 18
#     country: t.Literal["Netherlands", "Other"]
#
# class Person(TypedConfig, Interactive):
#     info: Example
#     extra: dict
#
#
# if __name__ == "__main__":
#     person = Person.interactive()
#     print(asdict(person))
