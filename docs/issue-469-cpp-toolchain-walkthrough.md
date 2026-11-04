# C/C++ toolchain in the Lima VM

<!-- Captured with Showboat 0.6.1 on 2026-11-04 -->
<!-- showboat-id: ebb3933d-7120-42e5-ab30-25a866396450 -->

Why: The Lima VM had the C compiler, Make, and libc headers, but lacked C++
compilation, standard CMake generators, and native debugging. This change
completes that local development workflow.

What changed: Fedora provides GCC/G++, GNU Make, CMake, GDB, Ninja, and
pkg-config. The additional RPMs are VM-only; readiness and
`devbox-toolchain-check` inspect them. Versions follow Fedora, not the manifest.

## Source map

- VM-only packages: `lima/fedora-packages.sh:79-90`.
- Operator checks: `lima/check_toolchain.py:91-100`.
- Readiness: `lima/probe-readiness.sh:201-212`, mirrored in `lima/devbox.yaml`.
- Agent guidance: `lima/agent-skills/devbox-tools/SKILL.md:249-281`.
- VM coverage: `tests/vm/test_provisioned_vm.py:81-230`.

Observed in a fully provisioned fresh L2 from this worktree. The staged source
mount was read-only; config and credential mount sources were empty, and no L1
credentials were passed into the L2. The task root and L2 were deleted after
Showboat verification.

```bash
limactl shell issue-469-cpp-toolchain-l2 -- bash -ceu '
set -euo pipefail
task_root="$(mktemp -d /tmp/opencode/issue-469-demo.XXXXXXXX)"
cleanup_task_dir() { rm -rf -- "${task_root:?}"; }
trap cleanup_task_dir EXIT

toolchain_output="$(devbox-toolchain-check)"
printf "%s\n" "$toolchain_output" \
  | grep -E "^OK: (gcc|g[+][+]|make|cmake|gdb|ninja|pkg-config):"
for tool in gcc g++ make cmake gdb ninja pkg-config; do
  command -v "$tool" >/dev/null 2>&1
  "$tool" --version >/dev/null 2>&1
done

mkdir -p "$task_root/c" "$task_root/cpp"
printf "%s\n" "#include <stdio.h>" \
  "int main(void) { puts(\"issue-469-c-ok\"); return 0; }" \
  >"$task_root/main.c"
printf "%s\n" "#include <iostream>" \
  "int main() { std::cout << \"issue-469-cpp-ok\\n\"; return 0; }" \
  >"$task_root/main.cpp"
gcc -g -Wall -Wextra -Werror -std=c17 "$task_root/main.c" -o "$task_root/c-app"
g++ -g -Wall -Wextra -Werror -std=c++17 "$task_root/main.cpp" -o "$task_root/cpp-app"
"$task_root/c-app" >"$task_root/c.out"
"$task_root/cpp-app" >"$task_root/cpp.out"
grep -qx "issue-469-c-ok" "$task_root/c.out"
grep -qx "issue-469-cpp-ok" "$task_root/cpp.out"
printf "%s\n" \
  "Direct GCC C compile/link/run: PASS" \
  "Direct G++ C++ compile/link/run: PASS"

gdb -batch -nx -q --return-child-result -ex run --args "$task_root/c-app" \
  >"$task_root/gdb-c.log" 2>&1
gdb -batch -nx -q --return-child-result -ex run --args "$task_root/cpp-app" \
  >"$task_root/gdb-cpp.log" 2>&1
grep -qx "issue-469-c-ok" "$task_root/gdb-c.log"
grep -qx "issue-469-cpp-ok" "$task_root/gdb-cpp.log"
printf "%s\n" "GDB batch execution of C and C++ programs: PASS"

printf "%s\n" "cmake_minimum_required(VERSION 3.20)" \
  "project(issue_469_c LANGUAGES C)" \
  "add_executable(c_app ../main.c)" >"$task_root/c/CMakeLists.txt"
printf "%s\n" "cmake_minimum_required(VERSION 3.20)" \
  "project(issue_469_cpp LANGUAGES CXX)" \
  "set(CMAKE_CXX_STANDARD 17)" \
  "set(CMAKE_CXX_STANDARD_REQUIRED ON)" \
  "add_executable(cpp_app ../main.cpp)" >"$task_root/cpp/CMakeLists.txt"
for language in c cpp; do
  case "$language" in
    c) app=c_app; expected=issue-469-c-ok ;;
    cpp) app=cpp_app; expected=issue-469-cpp-ok ;;
  esac
  for generator in "Unix Makefiles" Ninja; do
    case "$generator" in
      "Unix Makefiles") slug=makefiles ;;
      Ninja) slug=ninja ;;
    esac
    build_dir="$task_root/build-$language-$slug"
    if ! cmake -S "$task_root/$language" -B "$build_dir" -G "$generator" \
      >"$task_root/cmake.log" 2>&1; then
      cat "$task_root/cmake.log" >&2
      exit 1
    fi
    if ! cmake --build "$build_dir" --parallel >>"$task_root/cmake.log" 2>&1; then
      cat "$task_root/cmake.log" >&2
      exit 1
    fi
    "$build_dir/$app" >"$task_root/run.out"
    grep -qx "$expected" "$task_root/run.out"
    case "$generator" in
      "Unix Makefiles") test -f "$build_dir/Makefile" ;;
      Ninja) test -f "$build_dir/build.ninja" ;;
    esac
    printf "CMake %s with %s: PASS\n" "$language" "$generator"
  done
done
'
```

```output
OK: gcc: gcc (GCC) 16.2.1 20261029 (Red Hat 16.2.1-3)
OK: g++: g++ (GCC) 16.2.1 20261029 (Red Hat 16.2.1-3)
OK: make: GNU Make 4.4.1
OK: cmake: cmake version 4.3.0
OK: gdb: GNU gdb (Fedora Linux) 18.1-1.fc44
OK: ninja: 1.13.2
OK: pkg-config: 2.5.1
Direct GCC C compile/link/run: PASS
Direct G++ C++ compile/link/run: PASS
GDB batch execution of C and C++ programs: PASS
CMake c with Unix Makefiles: PASS
CMake c with Ninja: PASS
CMake cpp with Unix Makefiles: PASS
CMake cpp with Ninja: PASS
```

## Reproduce and clean up

Follow the fresh-L2 checklist in [`lima/README.md`](../lima/README.md) and the
task-isolation recipe in [`AGENTS.md`](../AGENTS.md). Create a private
`LIMA_HOME` with a staged read-only source mount and empty config/credential
sources, then replace the recorded instance name with the new L2's name. After
readiness, confirm `limactl shell <name> -- echo hello`, stop and delete the
instance, then remove the private task root only after deletion succeeds. The
demo instance and its temporary root have already been removed.
