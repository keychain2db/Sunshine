from pathlib import Path
import re
import sys


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(1)


def read(path: str) -> str:
    p = Path(path)
    if not p.exists():
        fail(f"Missing file: {path}")
    return p.read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old in text:
        return text.replace(old, new, 1)
    if new in text:
        return text
    fail(f"Could not apply edit: {label}")


def remove_once(text: str, old: str, label: str) -> str:
    return replace_once(text, old, "", label)


def insert_after(text: str, anchor: str, addition: str, label: str) -> str:
    if addition in text:
        return text
    if anchor not in text:
        fail(f"Could not find anchor for: {label}")
    return text.replace(anchor, anchor + addition, 1)


def insert_before(text: str, anchor: str, addition: str, label: str) -> str:
    if addition in text:
        return text
    if anchor not in text:
        fail(f"Could not find anchor for: {label}")
    return text.replace(anchor, addition + anchor, 1)


def insert_after_any(text: str, anchors: list[str], addition: str, label: str) -> str:
    if addition in text:
        return text
    for anchor in anchors:
        if anchor in text:
            return text.replace(anchor, anchor + addition, 1)
    fail(f"Could not find anchor for: {label}")


def insert_after_regex(text: str, pattern: str, addition: str, label: str) -> str:
    if addition in text:
        return text
    m = re.search(pattern, text, flags=re.MULTILINE)
    if not m:
        fail(f"Could not find regex anchor for: {label}")
    return text[:m.end()] + addition + text[m.end():]


def insert_after_last_include(text: str, addition: str, label: str) -> str:
    if addition in text:
        return text

    matches = list(re.finditer(r'^[ \t]*#include\s+[<"].+[>"].*(?:\r?\n)?', text, flags=re.MULTILINE))
    if not matches:
        fail(f"Could not find include block for: {label}")

    last = matches[-1]
    insert_pos = last.end()
    return text[:insert_pos] + addition + text[insert_pos:]


def remove_between(text: str, start: str, end: str, label: str) -> str:
    start_idx = text.find(start)
    if start_idx == -1:
        if end in text:
            return text
        fail(f"Could not find start marker for: {label}")

    end_idx = text.find(end, start_idx + len(start))
    if end_idx == -1:
        fail(f"Could not find end marker for: {label}")

    return text[:start_idx] + text[end_idx:]


def guarded_replace(target: str, replacement: str, anchors: list[str]) -> None:
    current = read(target)
    missing = [a for a in anchors if a not in current]
    if missing:
        print(f"{target} is not in the expected upstream form.", file=sys.stderr)
        print("Missing anchors:", file=sys.stderr)
        for item in missing:
            print(f"  - {item}", file=sys.stderr)
        sys.exit(1)

    replacement_text = read(replacement)
    write(target, replacement_text)
    print(f"Applied Duo replacement for {target}")


def edit_file(path: str, transform) -> None:
    original = read(path)
    updated = transform(original)
    write(path, updated)
    print(f"Applied Duo inline edits to {path}")

def transform_process(text: str) -> str:
    include_addition = (
        '\n'
        '// We have to include the Windows display header here for\n'
        '// RdpIddCaptureBuffer / RdpIddCaptureMode definitions.\n'
        '// boost/process must come before display.h on Windows due to WinSock.\n'
        'typedef long NTSTATUS;\n'
        '#include "platform/windows/display.h"\n'
    )

    # Insert after the last #include in the file, regardless of exact upstream include layout.
    if '#include "platform/windows/display.h"' not in text:
        text = insert_after_last_include(
            text,
            include_addition,
            "process.cpp include display.h",
        )

    execute_addition = """    // Puzzle together the current session's shared buffer name
    DWORD sessionId = 0;
    ProcessIdToSessionId(GetCurrentProcessId(), &sessionId);
    std::string sharedBufferName = "Global\\\\RdpIddCaptureBuffer" + std::to_string(sessionId);

    // Open the shared buffer
    HANDLE sharedBufferHandle = OpenFileMappingA(FILE_MAP_ALL_ACCESS, FALSE, sharedBufferName.c_str());

    // We managed to open the shared buffer handle
    if (sharedBufferHandle != NULL)
    {
      // Map the shared buffer
      platf::dxgi::PRdpIddCaptureBuffer sharedBuffer =
        (platf::dxgi::PRdpIddCaptureBuffer) MapViewOfFile(
          sharedBufferHandle,
          FILE_MAP_ALL_ACCESS,
          0,
          0,
          sizeof(platf::dxgi::RdpIddCaptureBuffer)
        );

      // We managed to map the shared buffer
      if (sharedBuffer != NULL)
      {
        // We need to adjust the mode
        if (sharedBuffer->Mode.Width != launch_session->width ||
            sharedBuffer->Mode.Height != launch_session->height ||
            sharedBuffer->Mode.RefreshRate != launch_session->fps ||
            (sharedBuffer->Mode.IsHDRSupported && sharedBuffer->Mode.HDR != launch_session->enable_hdr))
        {
          // Copy over the parameters
          sharedBuffer->Mode.Width = launch_session->width;
          sharedBuffer->Mode.Height = launch_session->height;
          sharedBuffer->Mode.RefreshRate = launch_session->fps;
          sharedBuffer->Mode.HDR = sharedBuffer->Mode.IsHDRSupported && launch_session->enable_hdr;

          // Request a mode change
          sharedBuffer->ModeChangePending = TRUE;
        }

        // Unmap the shared buffer
        UnmapViewOfFile(sharedBuffer);
      }

      // Close the shared buffer handle
      CloseHandle(sharedBufferHandle);
    }

"""

    if 'std::string sharedBufferName = "Global\\\\RdpIddCaptureBuffer"' not in text:
        text = insert_after_regex(
            text,
            r'(^[ \t]*int[ \t]+proc_t::execute\s*\(\s*int\s+app_id\s*,\s*std::shared_ptr<rtsp_stream::launch_session_t>\s+launch_session\s*\)\s*\{\r?\n)',
            execute_addition,
            "process.cpp shared buffer mode change",
        )

    return text


def normalize_display_base_cpp(text: str) -> str:
    # Normalize Boost.Process usage and include ordering so WinSock stays happy.
    text = text.replace("#include <boost/process.hpp>\r\n", "")
    text = text.replace("#include <boost/process.hpp>\n", "")
    text = text.replace("#include <boost/process/v1.hpp>\r\n", "")
    text = text.replace("#include <boost/process/v1.hpp>\n", "")

    text = text.replace('#include "display.h"\r\n', "")
    text = text.replace('#include "display.h"\n', "")

    text = text.replace('#include "utf_utils.h"\r\n', "")
    text = text.replace('#include "utf_utils.h"\n', "")

    # Remove direct Windows includes if the replacement brought them in.
    text = text.replace("#include <Windows.h>\r\n", "")
    text = text.replace("#include <Windows.h>\n", "")
    text = text.replace("#include <windows.h>\r\n", "")
    text = text.replace("#include <windows.h>\n", "")
    text = text.replace("#include <shellscalingapi.h>\r\n", "")
    text = text.replace("#include <shellscalingapi.h>\n", "")
    text = text.replace("#include <shtypes.h>\r\n", "")
    text = text.replace("#include <shtypes.h>\n", "")

    text = insert_after_any(
        text,
        [
            "#include <boost/algorithm/string/join.hpp>\n",
            "#include <boost/algorithm/string/join.hpp>\r\n",
        ],
        '#include <boost/process/v1.hpp>\n#include <MinHook.h>\n\n#include "utf_utils.h"\n',
        "display_base.cpp include normalization",
    )

    # Remove duplicate MinHook include if it already existed elsewhere.
    text = text.replace('#include <MinHook.h>\n#include <MinHook.h>\n', '#include <MinHook.h>\n')
    text = text.replace('#include <MinHook.h>\r\n#include <MinHook.h>\r\n', '#include <MinHook.h>\r\n')

    text = replace_once(
        text,
        'typedef long NTSTATUS;\n',
        'typedef long NTSTATUS;\n\n#include "display.h"\n',
        "display_base.cpp reinsert display.h after NTSTATUS",
    )

    text = text.replace("namespace bp = boost::process::v1;", "namespace bp = boost::process;")
    text = text.replace("namespace bp = boost::process::v1", "namespace bp = boost::process")

    text = text.replace("to_utf8(", "utf_utils::to_utf8(")
    text = text.replace("from_utf8(", "utf_utils::from_utf8(")

    # Avoid double-qualification if already fixed.
    text = text.replace("utf_utils::utf_utils::to_utf8(", "utf_utils::to_utf8(")
    text = text.replace("utf_utils::utf_utils::from_utf8(", "utf_utils::from_utf8(")

    return text


def normalize_display_vram_cpp(text: str) -> str:
    if '#include "utf_utils.h"' not in text:
        text = insert_after_any(
            text,
            [
                '#include "src/video.h"\n',
                '#include "src/video.h"\r\n',
                '#include "src/nvenc/nvenc_utils.h"\n',
                '#include "src/nvenc/nvenc_utils.h"\r\n',
            ],
            '#include "utf_utils.h"\n',
            "display_vram.cpp add utf_utils include",
        )

    text = text.replace("auto wFile = from_utf8(file);", "auto wFile = utf_utils::from_utf8(file);")
    text = text.replace("auto wFile = utf_utils::utf_utils::from_utf8(file);", "auto wFile = utf_utils::from_utf8(file);")

    text = text.replace(
        "auto color_vectors = ::video::color_vectors_from_colorspace(colorspace);",
        "auto color_vectors = ::video::color_vectors_from_colorspace(colorspace, true);",
    )
    text = text.replace(
        "color_vectors = ::video::new_color_vectors_from_colorspace(colorspace);",
        "color_vectors = ::video::color_vectors_from_colorspace(colorspace, false);",
    )
    text = text.replace(
        "auto default_color_vectors = ::video::color_vectors_from_colorspace(::video::colorspace_e::rec601, false);",
        "auto default_color_vectors = ::video::color_vectors_from_colorspace({::video::colorspace_e::rec601, false, 8}, true);",
    )

    return text


def transform_options(text: str) -> str:
    text = replace_once(
        text,
        'set(SUNSHINE_PUBLISHER_NAME "Third Party Publisher"',
        'set(SUNSHINE_PUBLISHER_NAME "Black-Seraph"',
        "options.cmake publisher name",
    )
    text = replace_once(
        text,
        'set(SUNSHINE_PUBLISHER_WEBSITE ""',
        'set(SUNSHINE_PUBLISHER_WEBSITE "https://www.black-seraph.com"',
        "options.cmake publisher website",
    )
    text = replace_once(
        text,
        'set(SUNSHINE_PUBLISHER_ISSUE_URL "https://app.lizardbyte.dev/support"',
        'set(SUNSHINE_PUBLISHER_ISSUE_URL "https://github.com/DuoStream/Duo/issues"',
        "options.cmake issue url",
    )
    text = replace_once(
        text,
        'option(BUILD_DOCS "Build documentation" ON)',
        'option(BUILD_DOCS "Build documentation" OFF)',
        "options.cmake docs off",
    )
    text = replace_once(
        text,
        'option(BUILD_TESTS "Build tests" ON)',
        'option(BUILD_TESTS "Build tests" OFF)',
        "options.cmake tests off",
    )
    return text


def transform_main(text: str) -> str:
    text = replace_once(
        text,
        """#ifdef _WIN32
  // Modify relevant NVIDIA control panel settings if the system has corresponding gpu
  if (nvprefs_instance.load()) {
    // Restore global settings to the undo file left by improper termination of sunshine.exe
    nvprefs_instance.restore_from_and_delete_undo_file_if_exists();
    // Modify application settings for sunshine.exe
    nvprefs_instance.modify_application_profile();
    // Modify global settings, undo file is produced in the process to restore after improper termination
    nvprefs_instance.modify_global_profile();
    // Unload dynamic library to survive driver re-installation
    nvprefs_instance.unload();
  }
""",
        """#ifdef _WIN32
  /*
  // Modify relevant NVIDIA control panel settings if the system has corresponding gpu
  if (nvprefs_instance.load()) {
    // Restore global settings to the undo file left by improper termination of sunshine.exe
    nvprefs_instance.restore_from_and_delete_undo_file_if_exists();
    // Modify application settings for sunshine.exe
    nvprefs_instance.modify_application_profile();
    // Modify global settings, undo file is produced in the process to restore after improper termination
    nvprefs_instance.modify_global_profile();
    // Unload dynamic library to survive driver re-installation
    nvprefs_instance.unload();
  }
  */
""",
        "main.cpp disable nvprefs block",
    )
    text = replace_once(
        text,
        """  if (video::probe_encoders()) {
    BOOST_LOG(error) << "Video failed to find working encoder"sv;
  }
""",
        """  if (video::probe_encoders()) {
    BOOST_LOG(error) << "Video failed to find working encoder"sv;
    return -1;
  }
""",
        "main.cpp fail when no encoder",
    )
    return text


def transform_process(text: str) -> str:
    include_addition = (
        '\n'
        '  // We have to include boost/process.hpp before display.h due to WinSock.h,\n'
        '  // but that prevents the definition of NTSTATUS so we must define it ourself.\n'
        '  typedef long NTSTATUS;\n\n'
        '  // RdpIddCaptureBuffer & RdpIddCaptureMode structures\n'
        '  #include "platform/windows/display.h"\n'
    )

    text = insert_after_any(
        text,
        [
            '  #include "platform/windows/misc.h"\n',
            '  #include "platform/windows/misc.h"\r\n',
            '#include "platform/windows/misc.h"\n',
            '#include "platform/windows/misc.h"\r\n',
            '  #include "platform/windows/misc.h"\n\n',
            '  #include "platform/windows/misc.h"\r\n\r\n',
        ],
        include_addition,
        "process.cpp include display.h",
    )

    execute_addition = """    // Puzzle together the current session's shared buffer name
    DWORD sessionId = 0;
    ProcessIdToSessionId(GetCurrentProcessId(), &sessionId);
    std::string sharedBufferName = "Global\\\\RdpIddCaptureBuffer" + std::to_string(sessionId);

    // Open the shared buffer
    HANDLE sharedBufferHandle = OpenFileMappingA(FILE_MAP_ALL_ACCESS, FALSE, sharedBufferName.c_str());

    // We managed to open the shared buffer handle
    if (sharedBufferHandle != NULL)
    {
      // Map the shared buffer
      platf::dxgi::PRdpIddCaptureBuffer sharedBuffer = (platf::dxgi::PRdpIddCaptureBuffer)MapViewOfFile(sharedBufferHandle, FILE_MAP_ALL_ACCESS, 0, 0, sizeof(platf::dxgi::RdpIddCaptureBuffer));

      // We managed to map the shared buffer
      if (sharedBuffer != NULL)
      {
        // We need to adjust the mode
        if (sharedBuffer->Mode.Width != launch_session->width || sharedBuffer->Mode.Height != launch_session->height || sharedBuffer->Mode.RefreshRate != launch_session->fps || (sharedBuffer->Mode.IsHDRSupported && sharedBuffer->Mode.HDR != launch_session->enable_hdr))
        {
          // Copy over the parameters
          sharedBuffer->Mode.Width = launch_session->width;
          sharedBuffer->Mode.Height = launch_session->height;
          sharedBuffer->Mode.RefreshRate = launch_session->fps;
          sharedBuffer->Mode.HDR = sharedBuffer->Mode.IsHDRSupported && launch_session->enable_hdr;

          // Request a mode change
          sharedBuffer->ModeChangePending = TRUE;
        }

        // Unmap the shared buffer
        UnmapViewOfFile(sharedBuffer);
      }

      // Close the shared buffer handle
      CloseHandle(sharedBufferHandle);
    }

"""

    text = insert_after_any(
        text,
        [
            "  int proc_t::execute(int app_id, std::shared_ptr<rtsp_stream::launch_session_t> launch_session) {\n",
            "  int proc_t::execute(int app_id, std::shared_ptr<rtsp_stream::launch_session_t> launch_session) {\r\n",
            "int proc_t::execute(int app_id, std::shared_ptr<rtsp_stream::launch_session_t> launch_session) {\n",
            "int proc_t::execute(int app_id, std::shared_ptr<rtsp_stream::launch_session_t> launch_session) {\r\n",
        ],
        execute_addition,
        "process.cpp shared buffer mode change",
    )

    return text


def transform_video(text: str) -> str:
    text = remove_once(
        text,
        "      bool artificial_reinit = false;\n\n",
        "video.cpp remove artificial_reinit declaration",
    )
    text = replace_once(
        text,
        """        if (switch_display_event->peek()) {
          artificial_reinit = true;
          return false;
        }
""",
        """        if (switch_display_event->peek()) {
          return false;
        }
""",
        "video.cpp remove artificial_reinit set",
    )
    text = remove_once(
        text,
        """      if (artificial_reinit && status != platf::capture_e::error) {
        status = platf::capture_e::reinit;

        artificial_reinit = false;
      }

""",
        "video.cpp remove artificial_reinit block",
    )
    text = insert_before(
        text,
        "      session->request_normal_frame();\n",
        """      // The frame encoded event handle
      static HANDLE frameEncodedEventHandle;

      // The frame encoded event hasn't been opened yet
      if (frameEncodedEventHandle == NULL)
      {
        // Open the frame ready event
        DWORD sessionId = 0;
        ProcessIdToSessionId(GetCurrentProcessId(), &sessionId);
        char frameEncodedEventName[32];
        sprintf(frameEncodedEventName, "Global\\\\DuoIdd%uFrameEncoded", (unsigned int)sessionId);
        frameEncodedEventHandle = OpenEventA(EVENT_ALL_ACCESS, FALSE, frameEncodedEventName);
      }

      // We've got an event we can signal
      if (frameEncodedEventHandle != NULL)
      {
        // Signal the event
        SetEvent(frameEncodedEventHandle);
      }

""",
        "video.cpp signal frame encoded event",
    )
    return text


def transform_audio_video_vue(text: str) -> str:
    # Remove platform-specific imports not needed by Duo.
    for line, label in [
        ('import {$tp} from "../../platform-i18n"\n', "AudioVideo.vue remove platform i18n import"),
        ('import PlatformLayout from "../../PlatformLayout.vue"\n', "AudioVideo.vue remove PlatformLayout import"),
        ('import AdapterNameSelector from "./audiovideo/AdapterNameSelector.vue"\n', "AudioVideo.vue remove AdapterNameSelector import"),
        ('import DisplayOutputSelector from "./audiovideo/DisplayOutputSelector.vue"\n', "AudioVideo.vue remove DisplayOutputSelector import"),
        ('import DisplayDeviceOptions from "./audiovideo/DisplayDeviceOptions.vue";\n', "AudioVideo.vue remove DisplayDeviceOptions import"),
    ]:
        if line in text:
            text = remove_once(text, line, label)

    text = remove_between(
        text,
        "    <!-- Audio Sink -->\n",
        "    <!-- Disable Audio -->\n",
        "AudioVideo.vue remove audio sink/device sections",
    )

    if """
    <AdapterNameSelector
        :platform="platform"
        :config="config"
    />

""" in text:
        text = remove_once(
            text,
            """
    <AdapterNameSelector
        :platform="platform"
        :config="config"
    />

""",
            "AudioVideo.vue remove AdapterNameSelector block",
        )

    if """    <DisplayOutputSelector
      :platform="platform"
      :config="config"
    />

""" in text:
        text = remove_once(
            text,
            """    <DisplayOutputSelector
      :platform="platform"
      :config="config"
    />

""",
            "AudioVideo.vue remove DisplayOutputSelector block",
        )

    if """    <DisplayDeviceOptions
      :platform="platform"
      :config="config"
    />

""" in text:
        text = remove_once(
            text,
            """    <DisplayDeviceOptions
      :platform="platform"
      :config="config"
    />

""",
            "AudioVideo.vue remove DisplayDeviceOptions block",
        )

    return text


def transform_index_html(text: str) -> str:
    text = replace_once(
        text,
        """      installedVersionNotStable() {
        if (!this.githubVersion || !this.version) {
          return false;
        }
        return this.version.isGreater(this.githubVersion);
      },
""",
        """      installedVersionNotStable() {
        return false;
      },
""",
        "index.html installedVersionNotStable",
    )
    text = replace_once(
        text,
        """      stableBuildAvailable() {
        if (!this.githubVersion || !this.version) {
          return false;
        }
        return this.githubVersion.isGreater(this.version);
      },
""",
        """      stableBuildAvailable() {
        return false;
      },
""",
        "index.html stableBuildAvailable",
    )
    text = replace_once(
        text,
        """      preReleaseBuildAvailable() {
        if (!this.preReleaseVersion || !this.githubVersion || !this.version) {
          return false;
        }
        return this.preReleaseVersion.isGreater(this.version) && this.preReleaseVersion.isGreater(this.githubVersion);
      },
""",
        """      preReleaseBuildAvailable() {
        return false;
      },
""",
        "index.html preReleaseBuildAvailable",
    )
    text = replace_once(
        text,
        """      buildVersionIsDirty() {
        return this.version.version?.split(".").length === 5 &&
          this.version.version.indexOf("dirty") !== -1
      },
""",
        """      buildVersionIsDirty() {
        return false;
      },
""",
        "index.html buildVersionIsDirty",
    )
    return text


def main() -> None:
    # Large files: guarded replacement
    guarded_replace(
        "src/platform/windows/display.h",
        ".github/duo/windows/display.h",
        [
            "#include <winrt/windows.graphics.capture.h>",
            "class display_ddup_ram_t: public display_ram_t",
            "class wgc_capture_t {",
        ],
    )

    guarded_replace(
        "src/platform/windows/display_base.cpp",
        ".github/duo/windows/display_base.cpp",
        [
            "#include <boost/process/v1.hpp>",
            "#include <MinHook.h>",
            "NTSTATUS __stdcall NtGdiDdDDIGetCachedHybridQueryValueHook",
            "bool test_dxgi_duplication(adapter_t &adapter, output_t &output, bool enumeration_only)",
            "syncThreadDesktop();",
        ],
    )

    guarded_replace(
        "src/platform/windows/display_ram.cpp",
        ".github/duo/windows/display_ram.cpp",
        [
            "capture_e display_ddup_ram_t::snapshot(",
            "capture_e display_ddup_ram_t::release_snapshot()",
            "int display_ddup_ram_t::init(",
        ],
    )

    guarded_replace(
        "src/platform/windows/display_vram.cpp",
        ".github/duo/windows/display_vram.cpp",
        [
            "#include <DirectXMath.h>",
            "capture_e display_ddup_vram_t::snapshot(",
            "capture_e display_wgc_vram_t::snapshot(",
            "int display_ddup_vram_t::init(",
            "std::unique_ptr<nvenc_encode_device_t> display_vram_t::make_nvenc_encode_device(",
            "compile_pixel_shader_helper(cursor_ps_normalize_white);",
        ],
    )

    guarded_replace(
        "src/platform/windows/input.cpp",
        ".github/duo/windows/input.cpp",
        [
            "#include <ViGEm/Client.h>",
            "class vigem_t {",
            "void CALLBACK x360_notify(",
            "void CALLBACK ds4_notify(",
            "void gamepad_update(input_t &input, int nr, const gamepad_state_t &gamepad_state) {",
            "void gamepad_touch(input_t &input, const gamepad_touch_t &touch) {",
            "std::vector<supported_gamepad_t> &supported_gamepads(input_t *input) {",
        ],
    )

    # Post-fix replacement files for current upstream API/layout differences
    edit_file("src/platform/windows/display_base.cpp", normalize_display_base_cpp)
    edit_file("src/platform/windows/display_vram.cpp", normalize_display_vram_cpp)

    # Small / medium files: anchored inline edits

    edit_file(
        "cmake/compile_definitions/windows.cmake",
        lambda text: remove_once(
            replace_once(
                text,
                'set(CMAKE_EXE_LINKER_FLAGS "${CMAKE_EXE_LINKER_FLAGS} -static")\n',
                'set(CMAKE_EXE_LINKER_FLAGS "${CMAKE_EXE_LINKER_FLAGS} -static")\n\n'
                '# fix windres command line length issues\n'
                'set(CMAKE_RC_COMPILE_OBJECT "<CMAKE_RC_COMPILER> -O coff <DEFINES> <FLAGS> <SOURCE> <OBJECT>")\n',
                "windows.cmake windres fix",
            ),
            '        "${CMAKE_SOURCE_DIR}/src/platform/windows/display_wgc.cpp"\n',
            "windows.cmake remove display_wgc.cpp",
        ),
    )

    edit_file(
        "cmake/prep/constants.cmake",
        lambda text: replace_once(
            text,
            "set(SUNSHINE_TRAY 1)",
            "set(SUNSHINE_TRAY 0)",
            "constants.cmake disable tray",
        ),
    )

    edit_file("cmake/prep/options.cmake", transform_options)

    edit_file(
        "src/display_device.cpp",
        lambda text: replace_once(
            text,
            """  void configure_display(const SingleDisplayConfiguration &config) {
    std::lock_guard lock {DD_DATA.mutex};
    if (!DD_DATA.sm_instance) {
      // Platform is not supported, nothing to do.
      return;
    }

    DD_DATA.sm_instance->schedule([config](auto &settings_iface, auto &stop_token) {
      // We only want to keep retrying in case of a transient errors.
      // In other cases, when we either fail or succeed we just want to stop...
      if (settings_iface.applySettings(config) != SettingsManagerInterface::ApplyResult::ApiTemporarilyUnavailable) {
        stop_token.requestStop();
      }
    },
                                  {.m_sleep_durations = {DEFAULT_RETRY_INTERVAL}});
  }
""",
            """  void configure_display(const SingleDisplayConfiguration &config) {
    // We can't directly configure the display in remote sessions
    return;
  }
""",
            "display_device.cpp configure_display",
        ),
    )

    edit_file("src/main.cpp", transform_main)

    edit_file(
        "src/platform/windows/audio.cpp",
        lambda text: replace_once(
            text,
            """    int set_sink(const std::string &sink) override {
      auto device_id = set_format(sink);
      if (!device_id) {
        return -1;
      }

      int failure {};
      for (int x = 0; x < (int) ERole_enum_count; ++x) {
        auto status = policy->SetDefaultEndpoint(device_id->c_str(), (ERole) x);
        if (status) {
          // Depending on the format of the string, we could get either of these errors
          if (status == HRESULT_FROM_WIN32(ERROR_NOT_FOUND) || status == E_INVALIDARG) {
            BOOST_LOG(warning) << "Audio sink not found: "sv << sink;
          } else {
            BOOST_LOG(warning) << "Couldn't set ["sv << sink << "] to role ["sv << x << "]: 0x"sv << util::hex(status).to_string_view();
          }

          ++failure;
        }
      }

      // Remember the assigned sink name, so we have it for later if we need to set it
      // back after another application changes it
      if (!failure) {
        assigned_sink = sink;
      }

      return failure;
    }
""",
            """    int set_sink(const std::string &sink) override {
      // We can't directly configure the sink in remote sessions
      return 0;
    }
""",
            "audio.cpp set_sink",
        ),
    )

    edit_file(
        "src/platform/windows/misc.cpp",
        lambda text: replace_once(
            replace_once(
                text,
                "    DWORD consoleSessionId;",
                "    DWORD consoleSessionId = 0xFFFFFFFF;",
                "misc.cpp consoleSessionId init",
            ),
            "    consoleSessionId = WTSGetActiveConsoleSessionId();",
            "    ProcessIdToSessionId(GetCurrentProcessId(), &consoleSessionId);",
            "misc.cpp session id source",
        ),
    )

    edit_file("src/process.cpp", transform_process)
    edit_file("src/video.cpp", transform_video)

    edit_file(
        "src_assets/common/assets/web/Navbar.vue",
        lambda text: remove_once(
            remove_once(
                text,
                "import { initDiscord } from '@lizardbyte/shared-web/src/js/discord.js'\n",
                "Navbar.vue remove Discord import",
            ),
            "    initDiscord();\n",
            "Navbar.vue remove Discord init",
        ),
    )

    edit_file(
        "src_assets/common/assets/web/configs/tabs/Advanced.vue",
        lambda text: remove_between(
            text,
            "    <!-- Capture -->\n",
            "    <!-- Encoder -->\n",
            "Advanced.vue remove capture section",
        ),
    )

    edit_file(
        "src_assets/common/assets/web/configs/tabs/AudioVideo.vue",
        transform_audio_video_vue,
    )

    edit_file(
        "src_assets/common/assets/web/configs/tabs/General.vue",
        lambda text: remove_once(
            text,
            """
    <!-- Notify Pre-Releases -->
    <Checkbox class="mb-3"
              id="notify_pre_releases"
              locale-prefix="config"
              v-model="config.notify_pre_releases"
              default="false"
    ></Checkbox>
""",
            "General.vue remove prerelease checkbox",
        ),
    )

    edit_file(
        "src_assets/common/assets/web/configs/tabs/Inputs.vue",
        lambda text: remove_between(
            text,
            "    <!-- Emulated Gamepad Type -->\n",
            "    <!-- Home/Guide Button Emulation Timeout -->\n",
            "Inputs.vue remove gamepad UI block",
        ),
    )

    edit_file(
        "src_assets/common/assets/web/configs/tabs/audiovideo/DisplayModesSettings.vue",
        lambda text: remove_once(
            text,
            """
  <!--minimum_fps_target-->
  <div class="mb-3">
    <label for="minimum_fps_target" class="form-label">{{ $t("config.minimum_fps_target") }}</label>
    <input type="number" min="0" max="1000" class="form-control" id="minimum_fps_target" placeholder="0" v-model="config.minimum_fps_target" />
    <div class="form-text">{{ $t("config.minimum_fps_target_desc") }}</div>
  </div>
""",
            "DisplayModesSettings.vue remove minimum_fps_target",
        ),
    )

    edit_file("src_assets/common/assets/web/index.html", transform_index_html)

    print("All Duo changes applied successfully")


if __name__ == "__main__":
    main()