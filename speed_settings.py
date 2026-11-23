"""Native service-tier labels and advertised Standard/Fast choices."""


def _canonical(value):
    if value is None:
        return 'default'
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip().lower()
    if value in ('default', 'standard'):
        return 'default'
    if value in ('fast', 'priority'):
        return 'fast'
    return value


def current_speed(settings):
    """Missing metadata is unknown; an explicitly returned null is Standard."""
    if not isinstance(settings, dict) or 'serviceTier' not in settings:
        return None
    return _canonical(settings['serviceTier'])


def speed_label(settings):
    value = current_speed(settings)
    if value is None:
        return '未知（Codex 未返回速度）'
    return {'default': 'Standard', 'fast': 'Fast'}.get(value, ' '.join(value.split())[:60])


def speed_choices(model):
    """Only enable Fast when the model catalog explicitly advertises it.

    Older servers expose additionalSpeedTiers instead of serviceTiers. When
    present, the newer catalog takes precedence over the deprecated field.
    Standard always uses an explicit default override, never inherited null.
    """
    choices = [{'id': 'default', 'label': 'Standard'}]
    if not isinstance(model, dict):
        return choices
    tiers = model.get('serviceTiers')
    if 'serviceTiers' in model:
        identifiers = ([entry.get('id') for entry in tiers if isinstance(entry, dict)]
                       if isinstance(tiers, list) else [])
    else:
        identifiers = model.get('additionalSpeedTiers', [])
        if not isinstance(identifiers, list):
            identifiers = []
    # Prefer the current spelling if both are advertised; preserve an older
    # catalog's priority spelling in the write request.
    for candidate in ('fast', 'priority'):
        if candidate in identifiers:
            choices.append({'id': candidate, 'label': '⚡ Fast'})
            break
    return choices


def speed_matches(settings, desired):
    actual = current_speed(settings)
    expected = _canonical(desired)
    return actual is not None and expected in ('default', 'fast') and actual == expected
