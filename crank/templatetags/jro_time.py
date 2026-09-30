# Copyright (c) 2024 Isaac Adams
# Licensed under the MIT License. See LICENSE file in the project root for full license information.
"""Template filter that renders ISO timestamps compactly for the operations page."""

from datetime import timezone as dt_timezone

from django import template
from django.utils.dateparse import parse_datetime
from django.utils.html import format_html

register = template.Library()


@register.filter
def jro_time(value):
    """Render an ISO-8601 string as ``YYYY-MM-DD HH:MM UTC`` in a ``<time>`` element.

    The full ISO value stays available in ``datetime`` and ``title``. Empty input
    renders as an em dash; unparseable input renders as the escaped raw value.
    """
    if not value:
        return "—"
    parsed = parse_datetime(str(value))
    if parsed is None:
        return format_html("{}", value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt_timezone.utc)
    return format_html(
        '<time datetime="{}" title="{}">{}</time>',
        str(value),
        str(value),
        parsed.strftime("%Y-%m-%d %H:%M UTC"),
    )
