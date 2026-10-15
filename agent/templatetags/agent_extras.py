from django import template

register = template.Library()


@register.filter
def mmss(seconds):
    """90 -> "1:30". Blank for a missing value."""
    if seconds in (None, ""):
        return ""
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


@register.filter
def humanize_key(value):
    """"adjuster_name" -> "adjuster name"."""
    return str(value).replace("_", " ")
