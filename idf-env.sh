# Loads ESP-IDF (5.5.x) into the current shell:  . ./idf-env.sh
# It uses $IDF_PATH if it's set, and ~/esp/esp-idf otherwise. If ESP-IDF's Python environment isn't the first python3 on
# your PATH, set IDF_PYTHON_ENV_PATH to it (for example ~/.espressif/python_env/idf5.5_py3.13_env); otherwise the newest
# one installed for ESP-IDF 5.5 is used.
IDF_PATH="${IDF_PATH:-$HOME/esp/esp-idf}"
if [ -z "${IDF_PYTHON_ENV_PATH:-}" ]; then
    for env in "$HOME"/.espressif/python_env/idf5.5_py3.*_env; do
        [ -d "$env" ] && IDF_PYTHON_ENV_PATH="$env"
    done
fi
[ -n "${IDF_PYTHON_ENV_PATH:-}" ] && export IDF_PYTHON_ENV_PATH
if [ ! -f "$IDF_PATH/export.sh" ]; then
    echo "ESP-IDF isn't at $IDF_PATH: install ESP-IDF 5.5 (https://docs.espressif.com/projects/esp-idf/) or set IDF_PATH." >&2
    return 1 2>/dev/null || exit 1
fi
export IDF_PATH
. "$IDF_PATH/export.sh" >/dev/null && echo "ESP-IDF ready: $(idf.py --version)"
