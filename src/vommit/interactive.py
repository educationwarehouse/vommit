# pragma: exclude file

import dataclasses as dc
import typing as t

import questionary
from configuraptor import TypedConfig, load_into
from configuraptor.helpers import is_optional

DeriveDefaultSpec = t.TypedDict(
    "DeriveDefaultSpec",
    {"from": str, "with": str, "when": t.Any},
    total=False,
)


class AnnotatedMeta(t.TypedDict, total=False):
    label: str | None
    interactive: bool
    derive_default: DeriveDefaultSpec


@dc.dataclass(frozen=True)
class FieldSpec:
    name: str
    field_type: t.Any
    label: str
    interactive: bool = True
    meta: AnnotatedMeta | None = None


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


def _is_typed_config_type(
    _type: t.Any, with_optional: bool = True
) -> type[TypedConfig] | None:
    if with_optional and is_optional(_type):
        return next(
            (
                arg
                for arg in t.get_args(_type)
                if isinstance(arg, type) and issubclass(arg, TypedConfig)
            ),
            None,
        )
    else:
        return (
            _type
            if isinstance(_type, type) and issubclass(_type, TypedConfig)
            else None
        )


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


def _unwrap_annotated(tp: t.Any) -> tuple[t.Any, AnnotatedMeta]:
    if t.get_origin(tp) is not t.Annotated:
        return tp, AnnotatedMeta()

    args = t.get_args(tp)
    if not args:
        return tp, AnnotatedMeta()

    base_type = args[0]
    metadata = args[1:]
    meta: AnnotatedMeta = {}
    for item in metadata:
        if isinstance(item, str) and item.strip() and meta.get("label") is None:
            meta["label"] = item.strip()
        elif isinstance(item, dict):
            meta |= item
    return base_type, meta


def _enabled_field_name(fields: list[FieldSpec]) -> FieldSpec | None:
    return next(
        (
            field
            for field in fields
            if field.name == "enabled" and field.field_type is bool
        ),
        None,
    )


def _interactive_build[ConfigT: TypedConfig](
    cls: type[ConfigT],
    depth: int = 0,
    defaults: ConfigT | None = None,
    present_paths: set[str] | None = None,
    path_prefix: str = "",
) -> ConfigT:
    values: dict[str, t.Any] = {}
    hints = t.get_type_hints(cls, include_extras=True)
    fields: list[FieldSpec] = []
    for field_name, annotated_type in hints.items():
        field_type, meta = _unwrap_annotated(annotated_type)
        fields.append(
            FieldSpec(
                name=field_name,
                field_type=field_type,
                label=meta.get("label") or field_name,
                interactive=meta.get("interactive", True),
                meta=meta,
            )
        )

    enabled_field = _enabled_field_name(fields)
    if enabled_field:
        fields = sorted(fields, key=lambda field: field.name != enabled_field.name)

    _print_section_header(cls, depth)

    scalar_fields = [
        field
        for field in fields
        if (
            not _container_kind(field.field_type)
            and not _is_typed_config_type(field.field_type)
            and field.interactive
            and (
                present_paths is None
                or f"{path_prefix}.{field.name}".strip(".") not in present_paths
            )
        )
    ]
    total_scalar_fields = max(1, len(scalar_fields))
    prompt_step = 0

    skip_remaining = False
    for field in fields:
        field_name = field.name
        field_type = field.field_type
        field_path = f"{path_prefix}.{field_name}".strip(".")
        label = field.label
        has_default = defaults is not None and hasattr(defaults, field_name)
        default = getattr(defaults, field_name, None) if has_default else None
        if not has_default:
            has_default = hasattr(cls, field_name)
            default = getattr(cls, field_name, None)

        derive_default = (field.meta or {}).get("derive_default")
        if (
            isinstance(derive_default, dict)
            and isinstance(derive_default.get("from"), str)
            and isinstance(derive_default.get("with"), str)
            and derive_default["from"] in values
            and ("when" not in derive_default or default == derive_default.get("when"))
        ):
            derive_fn = getattr(cls, derive_default["with"], None)
            if callable(derive_fn):
                has_default = True
                default = derive_fn(values[derive_default["from"]])

        if skip_remaining:
            continue

        if not field.interactive:
            if has_default:
                values[field_name] = default
            continue

        container_kind = _container_kind(field_type)
        if container_kind:
            if has_default:
                values[field_name] = default
            else:
                values[field_name] = {} if container_kind is dict else []
            continue

        typed_config_type = _is_typed_config_type(field_type)

        if typed_config_type:
            nested_defaults = None
            if defaults is not None and hasattr(defaults, field_name):
                maybe_nested_defaults = getattr(defaults, field_name)
                if isinstance(maybe_nested_defaults, TypedConfig):
                    nested_defaults = maybe_nested_defaults
                elif is_optional(field_type) and not maybe_nested_defaults:
                    nested_defaults = load_into(typed_config_type, {"enabled": False})

            values[field_name] = _interactive_build(
                typed_config_type,
                depth + 1,
                nested_defaults,
                present_paths,
                field_path,
            )
            continue

        if (
            field.interactive
            and present_paths is not None
            and field_path in present_paths
        ):
            if has_default:
                values[field_name] = default
                if (
                    enabled_field
                    and field_name == enabled_field.name
                    and values[field_name] is False
                ):
                    skip_remaining = True
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
            and field_name == enabled_field.name
            and values[field_name] is False
        ):
            skip_remaining = True

    return load_into(cls, values)


def _interactive_key_paths(
    cls: type[TypedConfig],
    prefix: str = "",
) -> set[str]:
    paths: set[str] = set()
    hints = t.get_type_hints(cls, include_extras=True)

    for field_name, annotated_type in hints.items():
        field_type, meta = _unwrap_annotated(annotated_type)
        if not meta.get("interactive", True):
            continue

        field_path = f"{prefix}.{field_name}".strip(".")
        typed_config_type = _is_typed_config_type(field_type)
        if typed_config_type:
            paths |= _interactive_key_paths(typed_config_type, prefix=field_path)
            continue

        if _container_kind(field_type):
            continue

        paths.add(field_path)

    return paths


class InteractiveConfig(TypedConfig):
    @classmethod
    def interactive(
        cls,
        defaults: t.Self | None = None,
        present_paths: set[str] | None = None,
    ) -> t.Self:
        return _interactive_build(cls, defaults=defaults, present_paths=present_paths)

    @classmethod
    def interactive_key_paths(cls) -> set[str]:
        return _interactive_key_paths(cls)


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
