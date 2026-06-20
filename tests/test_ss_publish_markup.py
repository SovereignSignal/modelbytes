"""Tests for the Telegram-HTML → Slack mrkdwn converter (markup.py)."""
from ss_publish.markup import telegram_html_to_mrkdwn


def test_empty():
    assert telegram_html_to_mrkdwn("") == ""


def test_plain_passthrough_escaped():
    # bare < > & that aren't our tags get entity-escaped on the Slack side
    assert telegram_html_to_mrkdwn("a < b & c") == "a &lt; b &amp; c"


def test_bold_italic_code():
    assert telegram_html_to_mrkdwn("<b>x</b>") == "*x*"
    assert telegram_html_to_mrkdwn("<i>x</i>") == "_x_"
    assert telegram_html_to_mrkdwn("<code>x</code>") == "`x`"
    assert telegram_html_to_mrkdwn("<strong>x</strong>") == "*x*"
    assert telegram_html_to_mrkdwn("<em>x</em>") == "_x_"
    assert telegram_html_to_mrkdwn("<pre>x</pre>") == "`x`"


def test_link():
    out = telegram_html_to_mrkdwn('<a href="https://e.x/t">title</a>')
    assert out == "<https://e.x/t|title>"


def test_link_label_pipe_escaped():
    # a literal pipe in label would break mrkdwn; it's replaced with /
    out = telegram_html_to_mrkdwn('<a href="https://e.x">a|b</a>')
    assert out == "<https://e.x|a/b>"


def test_bare_href():
    out = telegram_html_to_mrkdwn('<a href="https://e.x"></a>')
    assert out == "<https://e.x>"


def test_br_becomes_newline():
    assert telegram_html_to_mrkdwn("line1<br>line2") == "line1\nline2"


def test_mixed_bundle():
    # the shape both channels actually emit
    src = '<b>Ship</b> — <a href="https://e.x/1">Aider 0.9</a> <i>ships</i> <code>x</code>'
    out = telegram_html_to_mrkdwn(src)
    assert out == "*Ship* — <https://e.x/1|Aider 0.9> _ships_ `x`"
