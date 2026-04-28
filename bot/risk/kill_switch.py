"""Kill-switch persistente em arquivo.

Quando o arquivo existe, qualquer chamada de execucao deve recusar.
Persistencia em arquivo (em vez de memoria) garante que reiniciar o
bot apos um kill *nao* o re-arma sozinho - precisa de intervencao
manual via CLI (`bot kill --reset`).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


class KillSwitch:
    """Flag-arquivo simples. is_active() le do disco a cada chamada."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def is_active(self) -> bool:
        return self._path.exists()

    def trigger(self, reason: str) -> None:
        """Ativa o kill-switch. Sobrescreve se ja estava ativo."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        self._path.write_text(f"{ts}: {reason}\n", encoding="utf-8")

    def reason(self) -> str | None:
        if not self._path.exists():
            return None
        try:
            return self._path.read_text(encoding="utf-8").strip()
        except OSError:
            return None

    def reset(self) -> None:
        """Desarma. Use so apos investigar a causa."""
        if self._path.exists():
            self._path.unlink()
