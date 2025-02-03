from __future__ import annotations

import enum
import logging
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel
from rich.console import Console

c = Console(width=256)
print = c.print

SANDBOX_TOKEN = ""
PROD_TOKEN = ""
TOKEN = SANDBOX_TOKEN


def _get_logger() -> logging.Logger:
    return logging.getLogger("zenodo")


def _get_zenodo_base_url(sandbox: bool) -> str:
    if sandbox:
        return "https://sandbox.zenodo.org/api"
    else:
        return "https://zenodo.org/api"


class Person(BaseModel):
    name: str
    affiliation: str | None
    orcid: str | None


class DepositionMetadata(BaseModel):
    upload_type: str
    title: str
    description: str
    creators: list[Person]
    access_right: str
    license: str
    version: str
    doi: str | None = None
    prereserve_doi: bool = False


class Deposition(BaseModel):
    metadata: DepositionMetadata


class DepositionTransaction:
    class _State(enum.Enum):
        Pending = "pending"
        Committed = "committed"
        Aborted = "aborted"
        Leaked = "leaked"

    def __init__(self, client: Client, deposition_json: dict[str, Any]) -> None:
        self._client = client
        self._deposition_json = deposition_json
        self._state = DepositionTransaction._State.Pending

    @property
    def deposition_id(self) -> str:
        return self._deposition_json["id"]

    @property
    def reserved_deposition_doi(self) -> str | None:
        if (obj := self._deposition_json['metadata'].get("prereserve_doi")) is None:
            return None
        return obj["doi"]

    @property
    def bucket_link(self) -> str:
        return self._deposition_json["links"]["bucket"]

    @property
    def pending(self) -> bool:
        return self._state == DepositionTransaction._State.Pending

    @property
    def committed(self) -> bool:
        return self._state == DepositionTransaction._State.Committed

    @property
    def aborted(self) -> bool:
        return self._state == DepositionTransaction._State.Aborted

    @property
    def leaked(self) -> bool:
        return self._state == DepositionTransaction._State.Leaked

    def commit(self) -> None:
        if not self.pending:
            _get_logger().warning(
                "Deposition %s is %s when committing.",
                self.deposition_id,
                self._state.value,
            )
            return
        self._client.commit_deposition(self.deposition_id)
        self._state = DepositionTransaction._State.Committed

    def abort(self) -> None:
        if self.committed:
            _get_logger().warning(
                "Aborting deposition %s which has been committed.", self.deposition_id
            )
        elif self.leaked:
            _get_logger().warning(
                "Deposition %s was leaked but abort was called; not aborting",
                self.deposition_id,
            )
            return
        elif self.aborted:
            _get_logger().warning(
                "Deposition %s was already aborted; not aborting again",
                self.deposition_id,
            )
            return
        self._client.abort_deposition(self.deposition_id)
        self._state = DepositionTransaction._State.Aborted

    def leak(self) -> None:
        self._expect_pending("leak")
        self._state = DepositionTransaction._State.Leaked

    def add_file(self, path: Path, *, name: str | None = None) -> httpx.Response:
        self._expect_pending("add a file to")
        return self._client.add_file_to_deposition(self.bucket_link, path, name=name)

    def _expect_pending(self, operation: str) -> None:
        if not self.pending:
            raise ValueError(
                f"Cannot {operation} deposition {self.deposition_id} "
                f"because the deposition has been {self._state.value}"
            )

    def __enter__(self) -> DepositionTransaction:  # noqa: PYI034
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        if not self.committed and not self.leaked:
            self.abort()


class Client:
    def __init__(self, *, sandbox: bool, token: str) -> None:
        base_url = _get_zenodo_base_url(sandbox)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }
        self._session = httpx.Client(base_url=base_url, headers=headers)

    def __enter__(self) -> Client:  # noqa: PYI034
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()

    def close(self) -> None:
        self._session.close()

    def get_depositions(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/deposit/depositions")
        response.raise_for_status()
        return response.json()

    def get_deposition(self, deposition_id: str) -> dict[str, Any]:
        response = self._request("GET", f"/deposit/depositions/{deposition_id}")
        response.raise_for_status()
        return response.json()

    def start_new_deposition(
        self, metadata: DepositionMetadata
    ) -> DepositionTransaction:
        response = self._request(
            "POST", "/deposit/depositions", model=Deposition(metadata=metadata)
        )
        response.raise_for_status()
        transaction = DepositionTransaction(self, response.json())
        _get_logger().info("Deposition started: %s", transaction.deposition_id)
        return transaction

    def continue_deposition(self, deposition_id: str) -> DepositionTransaction:
        deposition = self.get_deposition(deposition_id)
        if deposition['submitted'] or deposition['state'] in ('done', 'error'):
            raise ValueError(
                f"Cannot continue deposition {deposition_id}, "
                f"the deposition is not in progress."
            )
        transaction = DepositionTransaction(self, deposition)
        _get_logger().info("Continuing deposition %s", transaction.deposition_id)
        return transaction

    def commit_deposition(self, deposition_id: str) -> None:
        response = self._request(
            "POST", f"/deposit/depositions/{deposition_id}/actions/publish"
        )
        response.raise_for_status()
        _get_logger().info("Deposition committed: %s", deposition_id)

    def abort_deposition(self, deposition_id: str) -> None:
        response = self._request("DELETE", f"/deposit/depositions/{deposition_id}")
        response.raise_for_status()
        _get_logger().warning("Deposition aborted: %s", deposition_id)

    def add_file_to_deposition(
        self, bucket_link: str, path: Path, *, name: str | None
    ) -> httpx.Response:
        if name is None:
            name = path.name
        _get_logger().info(
            "Adding file to deposition on bucket link '%s': '%s' with name '%s'",
            bucket_link,
            path,
            name,
        )

        with path.open("rb") as file:
            response = self._request(
                "PUT",
                f"{bucket_link}/{name}",
                content=file.read(),
                headers={'Content-Type': 'application/octet-stream'},
            )

        if response.is_success:
            _get_logger().info("File successfully added: '%s'", path)
        else:
            _get_logger().error(
                "Failed to add file to deposition: '%s' (%s %s)",
                path,
                response.status_code,
                response.reason_phrase,
            )
        # TODO validate checksum returned by Zenodo
        return response

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        content: bytes | None = None,
        headers: dict[str, Any] | None = None,
        model: BaseModel | None = None,
    ) -> httpx.Response:
        if headers is None:
            headers = {}
        if model is not None:
            content = model.model_dump_json(exclude_none=True)
            headers["Content-Type"] = "application/json"
        return self._session.request(method, endpoint, content=content, headers=headers)


def main() -> None:
    logger = _get_logger()
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    logger.addHandler(handler)

    depo = DepositionMetadata(
        upload_type='software',
        title='Test Zenodo upload 3',
        description="Testing uploading to Zenodo",
        creators=[
            Person(
                name='Jan-Lukas Wynen',
                affiliation='European Spallation Source ERIC',
                orcid='0000-0002-3761-3201',
            )
        ],
        access_right='open',
        license='BSD-3-Clause',
        version='0.3',
        prereserve_doi=True,
    )

    with Client(sandbox=True, token=TOKEN) as client:
        with client.start_new_deposition(depo) as transaction:
            doi: str = transaction.reserved_deposition_doi
            depo_id = transaction.deposition_id
            # leak because the following code will be in a separate process on GH
            transaction.leak()

    # write CITATION.cff and make GH & PyPI releases here
    _get_logger().info("DOI: %s", doi)

    with Client(sandbox=True, token=TOKEN) as client:
        with client.continue_deposition(depo_id) as transaction:
            r = transaction.add_file(Path('CITATION.cff'))
            r.raise_for_status()
            transaction.leak()


if __name__ == "__main__":
    main()
