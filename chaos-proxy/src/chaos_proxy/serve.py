from __future__ import annotations

import threading
from collections.abc import Sequence

from chaos_proxy.controller import FaultController
from chaos_proxy.proxy import bind_servers
from chaos_proxy.settings import Settings


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    settings = Settings.from_env()
    controller = FaultController()
    proxy, admin, client = bind_servers(settings, controller)
    admin_thread = threading.Thread(target=admin.serve_forever, daemon=True)
    admin_thread.start()
    try:
        proxy.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        proxy.shutdown()
        admin.shutdown()
        proxy.server_close()
        admin.server_close()
        if getattr(proxy, "owns_upstream", False):
            client.close()
    return 0
