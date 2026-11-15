# System Dependencies

> System dependencies are operating-system resources required by an ARC service but not installed by Python's package manager.

## At a glance

| | |
|---|---|
| **Checked by** | ARC Core |
| **Installation check** | Forge, after package installation |
| **Startup check** | Pulse, before the service process is launched |
| **Detected automatically** | Missing ELF shared-library links in installed Python dependencies |
| **Declared manually** | Libraries loaded dynamically and external commands |
| **Status** | Proposed Core implementation |

## Behavior

Forge checks dependencies after installing a service. Missing system dependencies are reported, but the Python package remains installed and registered.

Pulse checks again immediately before starting the service. Missing required dependencies block that service from launching. Optional dependencies produce a warning and do not block startup.

The check is performed by Core before the Service Runner imports the service module. This is important because native libraries can fail during module import, before the service's `run()` or `start()` lifecycle code executes.

## Service metadata

A service may declare dependencies in `pyproject.toml`:

```toml
[tool.arc.service]
id = "example"
module = "example_service.service"

system_dependencies = [
    { kind = "shared-library", name = "libcuda.so.1", required = true },
    { kind = "command", name = "ffmpeg", required = false },
]
```

Supported kinds are `shared-library` and `command`.

## Detection boundaries

Automatic ELF inspection follows the installed distribution's transitive Python requirements and checks their `.so` files with `readelf` (or `objdump` as a fallback). It reads `DT_NEEDED`, `DT_RPATH`, and `DT_RUNPATH` metadata without executing the binaries. This detects unresolved linked dependencies such as `libcudart.so.13`.

Libraries loaded later through `dlopen`, external executables, hardware presence, and driver/runtime compatibility cannot be determined reliably from ELF link inspection alone. Declare those dependencies explicitly where possible. The checker reports missing dependencies; it does not install operating-system packages.
