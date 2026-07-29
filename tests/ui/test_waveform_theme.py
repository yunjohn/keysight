from PySide6.QtCore import Qt

from keysight_scope_app.ui.waveform_theme import (
    INSTRUMENT_DARK_THEME,
    InteractionTool,
    contrast_ratio,
)


def test_dark_theme_channel_styles_are_distinct_and_accessible() -> None:
    theme = INSTRUMENT_DARK_THEME
    styles = [theme.channel_style(f"CHANnel{index}") for index in range(1, 5)]
    assert len({style.color for style in styles}) == 4
    assert len({style.pen_style for style in styles}) == 4
    assert all(contrast_ratio(style.color, theme.plot_background) >= 4.5 for style in styles)


def test_interaction_tool_values_are_stable() -> None:
    assert {tool.value for tool in InteractionTool} == {
        "ZOOM",
        "PAN",
        "CURSOR_A",
        "CURSOR_B",
        "ANNOTATE",
    }


def test_reference_style_can_be_derived_from_channel() -> None:
    style = INSTRUMENT_DARK_THEME.channel_style("CHANnel2")
    assert style.color == "#56d364"
    assert style.pen_style is Qt.DashLine
