from dashboard.value_view import money, seconds


def test_dashboard_value_formatters():
    assert money(1234, decimals=0) == "$1,234"
    assert money(1.23456, decimals=2) == "$1.23"
    assert seconds(2500) == "2.50s"
