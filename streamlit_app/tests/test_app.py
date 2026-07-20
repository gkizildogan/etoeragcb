from streamlit.testing.v1 import AppTest


def test_login_shell_is_rendered() -> None:
    app = AppTest.from_file("app.py")
    app.run(timeout=10)
    assert not app.exception
    assert app.title[0].value == "Knowledge Assistant"
    assert app.text_input[0].label == "Email"
    assert app.text_input[1].label == "Password"
    assert app.button[0].label == "Sign in"
