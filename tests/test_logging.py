import logging

import pytest


@pytest.mark.django_db
class TestUnhandledExceptionsAreLoggedGlobally:
    @pytest.fixture(autouse=True)
    def boom_urlconf(self, settings, client):
        settings.ROOT_URLCONF = "tests._boom_urls"
        settings.DEBUG = False
        client.raise_request_exception = False

    def test_a_plain_view_exception_is_logged_with_its_traceback(self, client, caplog):
        caplog.set_level(logging.ERROR, logger="django.request")

        response = client.get("/plain-boom/")

        assert response.status_code == 500
        [record] = [r for r in caplog.records if r.name == "django.request"]
        assert record.levelname == "ERROR"
        assert record.exc_info[0] is ValueError
        assert "boom from a plain Django view" in str(record.exc_info[1])

    def test_a_drf_view_exception_is_also_logged(self, client, caplog):
        caplog.set_level(logging.ERROR, logger="django.request")

        response = client.get("/drf-boom/")

        assert response.status_code == 500
        [record] = [r for r in caplog.records if r.name == "django.request"]
        assert record.exc_info[0] is ValueError
        assert "boom from a DRF view" in str(record.exc_info[1])

    def test_the_traceback_is_not_exposed_to_the_client(self, client):
        response = client.get("/plain-boom/")

        body = response.content.decode()
        assert "ValueError" not in body
        assert "boom from a plain Django view" not in body

    def test_the_console_handler_writes_the_traceback_to_the_process_output(self, client):
        client.get("/plain-boom/")

        handler = logging.getLogger("django.request").handlers[0]
        assert type(handler) is logging.StreamHandler
        assert handler.filters == []


@pytest.mark.django_db
def test_an_expected_404_is_unaffected(client):
    response = client.get("/api/does-not-exist/")

    assert response.status_code == 404
