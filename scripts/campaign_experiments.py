"""Strict issuer-owned configuration for native ProgramBench probe tools."""

MAX_OPERATION_MS = 600_000


def validate_experiments(value):
    if value is None:
        return None
    if (not isinstance(value, dict)
            or set(value) != {"candidate", "interpreter", "timeout", "maxDuration", "prepare"}
            or value["candidate"] != "executable"
            or value["interpreter"] != "/usr/bin/python3"):
        raise ValueError("experiments require the fixed ProgramBench candidate and interpreter")
    prepare = value["prepare"]
    if (not isinstance(prepare, dict) or set(prepare) != {"argv", "timeout"}
            or prepare["argv"] != ["/bin/sh", "./compile.sh"]):
        raise ValueError("experiments prepare must execute /bin/sh ./compile.sh")
    if any(type(item) is not int or not 0 < item <= MAX_OPERATION_MS
           for item in (value["timeout"], value["maxDuration"], prepare["timeout"])):
        raise ValueError("experiment operation timeouts must be integers in 1..600000 milliseconds")
    if value["timeout"] > value["maxDuration"] or prepare["timeout"] > value["maxDuration"]:
        raise ValueError("experiment process timeouts must not exceed maxDuration")
    return {"candidate": value["candidate"], "interpreter": value["interpreter"],
            "timeout": value["timeout"], "maxDuration": value["maxDuration"],
            "prepare": {"argv": list(prepare["argv"]), "timeout": prepare["timeout"]}}


def validate_runtime_options(value):
    if not isinstance(value, dict) or set(value) != {"experiments"}:
        raise ValueError("runtime_options may contain only an explicit experiments object or null")
    return {"experiments": validate_experiments(value["experiments"])}
