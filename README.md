
# myWellnezz

![myWellnezz Image](mw.png?raw=true "myWellnezz")

myWellnezz is an application that helps users view and register for gym sessions at fitness centers using the MyWellness app. It is available in two modes: a **CLI** for interactive terminal use, and a **web interface** that runs as a background service.

## Features

- View available gym sessions
- Register / unregister for sessions
- Automatic booking when a spot opens up (auto-book)
- Multi-account support
- **Web interface** with real-time updates (WebSocket)
- **systemd service** for always-on background operation

## Disclaimer

This application is intended for informational and personal use only.
The creators are not affiliated with MyWellness or its parent company in any way.

---

## Web Interface (recommended)

### Automatic installation (systemd service)

```bash
git clone https://github.com/AeonDave/myWellnezz.git
cd myWellnezz
sudo ./install.sh
```

The installer:
1. Copies the project to `/opt/mywellnezz`
2. Creates a Python virtual environment and installs dependencies
3. Registers and starts the `mywellnezz@<user>` systemd service

Open **http://localhost:8080** in your browser, then follow the setup wizard:
1. Enter your MyWellness email and password
2. Select your gym

### Manual start (development / no systemd)

```bash
pip install -r requirements-web.txt
cd mywellnezz
uvicorn web_app:app --host 0.0.0.0 --port 8080
```

Or with Poetry:

```bash
poetry install
poetry run mywellnezz-web
```

### Service management

```bash
# Status
sudo systemctl status mywellnezz@$USER

# Logs (live)
journalctl -u mywellnezz@$USER -f

# Restart
sudo systemctl restart mywellnezz@$USER

# Uninstall
sudo ./install.sh --uninstall
```

### Configuration

Runtime settings are stored in `/etc/mywellnezz.conf`:

```ini
MYWELLNEZZ_HOST=0.0.0.0
MYWELLNEZZ_PORT=8080
```

---

## CLI Mode

### Requirements

- Python >= 3.11

### Run with Poetry

```bash
poetry install
poetry run mywellnezz
```

### Run manually

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cd mywellnezz && python main.py
```

### Create a standalone binary

```bash
poetry install
poetry run build
```

The binary will be generated in the `dist/` folder.

---

## Project structure

```
myWellnezz/
├── mywellnezz/
│   ├── main.py              # CLI entry point
│   ├── web_app.py           # Web service (FastAPI)
│   ├── templates/
│   │   └── index.html       # Web UI (Tailwind CSS + Alpine.js)
│   ├── app/
│   │   └── constants.py     # API endpoints, app metadata
│   ├── models/
│   │   ├── config.py        # Configuration persistence
│   │   ├── event.py         # Gym class model & booking logic
│   │   ├── facility.py      # Gym/facility model
│   │   ├── mywellnezz.py    # Core controller (async event loop)
│   │   └── usercontext.py   # Authentication & user profile
│   └── modules/
│       ├── http_calls.py    # Async HTTP client
│       ├── math_util.py     # Credential obfuscation
│       └── ...
├── mywellnezz.service       # systemd unit template
├── install.sh               # Automated installer
├── requirements-web.txt     # Web service dependencies
└── pyproject.toml
```

---

## Contributing

Contributions are welcome! Feel free to open a pull request.

## License

This project is licensed under the [Apache 2.0 License](LICENSE).
