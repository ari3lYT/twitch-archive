**English** · [Русский](deployment.ru.md)

# Dashboard deployment

1. Run the media worker in a fixed container named by `ARCHIVE_CONTAINER`; mount persistent archive
   and state directories. Its Dockerfile is in `archiver/`. Set UID/GID build arguments to match storage.
2. Create a private dashboard state directory and run `python tools/init_auth.py /path/to/panel-state`.
   The password is prompted without echo, hashed with a random salt and written with mode 0600.
3. Configure controller paths (`PANEL_BASE`, `STATE_ROOT`, `ARCHIVE_ROOT`) and the UID allowed to
   connect. Run `python dashboard/agent.py` with only the Docker/storage access it needs.
4. Configure the dashboard's `PANEL_STATE`, `MEDIA_ROOT`, `CONTROL_SOCKET` and exact `PUBLIC_ORIGIN`.
   Run `python dashboard/server.py` as an unprivileged user.
5. Reverse-proxy `/twitch/` to the loopback HTTP service using TLS. The session cookie is Secure
   and will not work over ordinary HTTP. Do not expose the agent socket or Docker socket to clients.

The included controller expects Docker. A native-worker deployment requires a controller adapter;
it is not automatically supported by substituting an arbitrary shell command. The repository does
not change NAS power schedules, system services or firewall settings.

For a screenshot-only local demo, `python tools/demo.py` starts the real dashboard with synthetic
records and a read-only mock controller. Its demo credentials are not valid in a deployed installation.
