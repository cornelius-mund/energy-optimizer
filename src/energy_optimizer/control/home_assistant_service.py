"""Call a Home Assistant service through its REST API."""

import httpx

from energy_optimizer.config import HomeAssistantConfiguration


class HomeAssistantServiceError(RuntimeError):
    """Raised when Home Assistant rejects or cannot receive a service call."""


def call_service(
    home_assistant: HomeAssistantConfiguration,
    client: httpx.Client,
    service: str,
    data: dict[str, object],
) -> None:
    domain, name = service.split(".", 1)
    base_url = str(home_assistant.base_url).rstrip("/")
    try:
        response = client.post(
            f"{base_url}/api/services/{domain}/{name}",
            json=data,
            headers={
                "Authorization": "Bearer " + home_assistant.token.get_secret_value()
            },
            timeout=home_assistant.timeout_seconds,
        )
    except httpx.HTTPError as error:
        raise HomeAssistantServiceError(
            f"Home Assistant service {service} failed: transport error"
        ) from error
    if response.status_code >= 400:
        raise HomeAssistantServiceError(
            f"Home Assistant service {service} returned HTTP {response.status_code}"
        )
