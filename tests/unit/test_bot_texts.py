from app.bot import texts


def test_user_supplied_names_are_html_escaped() -> None:
    message = texts.welcome_back("<b>Хакер</b>", "Кофейня <script>")
    assert "<b>Хакер</b>" not in message
    assert "&lt;b&gt;Хакер&lt;/b&gt;" in message
    assert "Кофейня &lt;script&gt;" in message


def test_welcome_without_project_asks_about_business() -> None:
    message = texts.welcome_back("Анна", None)
    assert "Анна" in message
    assert "бизнес" in message
