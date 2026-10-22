"""Keep the load balancer's process probe independent of public host names."""

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from apps.core.views import healthcheck


class HealthCheckMiddleware:
    """Answer only the public, read-only probe before CommonMiddleware checks its Host.

    ALB sends the container's changing private IP as Host. All application routes
    still go through Django's normal ALLOWED_HOSTS validation.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.path_info == "/api/healthcheck/" and request.method in {"GET", "HEAD"}:
            return healthcheck(request)
        return self.get_response(request)
