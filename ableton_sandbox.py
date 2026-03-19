"""
Ableton Sandbox - Code execution environment for Ableton VUI.

Instead of multi-turn LLM tool calls, the LLM generates Python code that
executes in this sandbox. MCP tools are exposed as async Python methods
on an `ableton` object. Data flows between tools inside the sandbox,
not back through the LLM context window.
"""

import asyncio
import io
import json
import sys
import textwrap
import types
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# Tool categories for grouping in the function catalog
# (reused from Trial_ableton_voice_control.py)
TOOL_CATEGORIES = {
    "Session / Info": ["get_session_info", "get_track_info"],
    "Playback / Transport": ["start_playback", "stop_playback", "set_tempo"],
    "Track Creation": ["create_midi_track", "create_audio_track", "delete_track",
                       "duplicate_track", "set_track_name", "set_track_color"],
    "Mixer": ["set_track_volume", "set_track_pan", "arm_track", "mute_track", "solo_track"],
    "Routing": ["get_track_routing_options", "set_track_output_routing", "set_track_input_routing",
                "set_track_input_channel", "set_track_output_channel", "set_track_monitoring"],
    "Grouping": ["create_track_group", "create_grouped_tracks"],
    "Sends / Returns": ["get_return_tracks_info", "get_track_sends", "set_track_send",
                        "create_return_track", "set_return_track_name", "delete_return_track",
                        "load_device_on_return_track",
                        "get_return_track_device_parameters", "set_return_track_device_parameter"],
    "Devices": ["get_device_parameters", "set_device_parameter", "load_instrument_or_effect",
                "load_device_by_name", "load_drum_kit"],
    "Clips": ["create_clip", "add_notes_to_clip", "set_clip_name", "fire_clip", "stop_clip"],
    "Browser": ["get_browser_tree", "get_browser_items_at_path", "get_all_browser_items",
                "fuzzy_search_browser", "load_browser_item"],
}


class AbletonToolError(Exception):
    """Raised when an MCP tool call fails."""
    pass


@dataclass
class SandboxResult:
    success: bool
    return_value: Any = None
    call_log: List[Dict] = field(default_factory=list)
    output: Optional[str] = None
    error: Optional[str] = None


class AbletonAPI:
    """Auto-generated async Python API wrapping all MCP tools.

    Each MCP tool becomes an async method on this object. The LLM-generated
    code calls these methods directly, keeping data flow local.
    """

    def __init__(self, mcp_session, tools: list):
        self._session = mcp_session
        self._call_log: List[Dict] = []
        self._generate_methods(tools)

    async def _call_tool(self, _tool_name: str, **kwargs) -> dict:
        """Call an MCP tool, parse JSON result, return Python dict."""
        result = await self._session.call_tool(_tool_name, arguments=kwargs)
        text = result.content[0].text if result.content else "{}"
        self._call_log.append({"tool": _tool_name, "args": kwargs, "result": text})

        # Check for error indicators
        if text.lower().startswith("error"):
            raise AbletonToolError(f"{_tool_name}: {text}")

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}

    def _generate_methods(self, tools: list):
        """Dynamically create one async method per MCP tool."""
        for tool in tools:
            tool_name = tool.name
            schema = tool.inputSchema if hasattr(tool, 'inputSchema') and tool.inputSchema else {}
            properties = schema.get("properties", {})
            required = set(schema.get("required", []))
            description = tool.description or f"Execute {tool_name}"

            # Build parameter list for the docstring
            param_parts = []
            for param_name, param_info in properties.items():
                param_type = param_info.get("type", "any")
                type_map = {"integer": "int", "number": "float", "string": "str",
                            "boolean": "bool", "array": "list", "object": "dict"}
                py_type = type_map.get(param_type, param_type)
                default_marker = "" if param_name in required else " (optional)"
                param_parts.append(f"{param_name}: {py_type}{default_marker}")

            sig_str = ", ".join(param_parts)
            short_desc = description.split('\n')[0].strip()

            # Create the async method using a closure with param validation
            def make_method(t_name, t_required):
                async def method(self_api, **kwargs):
                    # Validate required params before sending to MCP
                    missing = t_required - set(kwargs.keys())
                    if missing:
                        raise TypeError(f"{t_name}() missing required argument(s): {', '.join(sorted(missing))}")
                    return await self_api._call_tool(t_name, **kwargs)
                return method

            method = make_method(tool_name, required)
            method.__name__ = tool_name
            method.__qualname__ = f"AbletonAPI.{tool_name}"
            method.__doc__ = f"{short_desc}\n\nSignature: {tool_name}({sig_str}) -> dict"

            setattr(self, tool_name, types.MethodType(method, self))

    def reset_log(self):
        """Clear the call log for a new execution."""
        self._call_log.clear()


class AbletonSandbox:
    """Restricted execution environment for LLM-generated Python code.

    Only the `ableton` API object, `asyncio`, `json`, and safe builtins
    are available. No file access, imports, or network access.
    """

    SAFE_BUILTINS = {
        'range': range, 'len': len, 'enumerate': enumerate,
        'zip': zip, 'print': print, 'int': int, 'float': float,
        'str': str, 'list': list, 'dict': dict, 'tuple': tuple,
        'set': set, 'bool': bool, 'isinstance': isinstance,
        'min': min, 'max': max, 'sorted': sorted, 'reversed': reversed,
        'any': any, 'all': all, 'next': next, 'iter': iter,
        'round': round, 'abs': abs, 'map': map, 'filter': filter,
        'True': True, 'False': False, 'None': None,
        'ValueError': ValueError, 'TypeError': TypeError,
        'KeyError': KeyError, 'IndexError': IndexError,
        'Exception': Exception, 'StopIteration': StopIteration,
    }

    def __init__(self, mcp_session, tools: list):
        self.api = AbletonAPI(mcp_session, tools)

    async def execute(self, code: str, timeout: float = 30.0) -> SandboxResult:
        """Execute LLM-generated Python code with access to the ableton API."""
        self.api.reset_log()

        # Capture stdout
        captured_output = io.StringIO()

        sandbox_globals = {
            '__builtins__': self.SAFE_BUILTINS,
            'ableton': self.api,
            'asyncio': asyncio,
            'json': json,
            'print': lambda *args, **kwargs: print(*args, file=captured_output, **kwargs),
        }

        # Wrap code in an async function so `await` works
        wrapped = "async def __sandbox_main__():\n"
        wrapped += textwrap.indent(code, "    ")

        try:
            compiled = compile(wrapped, "<sandbox>", "exec")
            exec(compiled, sandbox_globals)
            result = await asyncio.wait_for(
                sandbox_globals["__sandbox_main__"](),
                timeout=timeout
            )
            return SandboxResult(
                success=True,
                return_value=result,
                call_log=self.api._call_log.copy(),
                output=captured_output.getvalue() or None,
            )
        except asyncio.TimeoutError:
            return SandboxResult(
                success=False,
                call_log=self.api._call_log.copy(),
                output=captured_output.getvalue() or None,
                error=f"Execution timed out after {timeout}s",
            )
        except AbletonToolError as e:
            return SandboxResult(
                success=False,
                call_log=self.api._call_log.copy(),
                output=captured_output.getvalue() or None,
                error=str(e),
            )
        except Exception as e:
            return SandboxResult(
                success=False,
                call_log=self.api._call_log.copy(),
                output=captured_output.getvalue() or None,
                error=f"{type(e).__name__}: {e}",
            )


def generate_function_catalog(tools: list) -> str:
    """Generate a compact function catalog for the LLM system prompt.

    Produces ~80-100 lines of text with function signatures grouped by category,
    replacing the thousands of tokens that 45 full JSON schemas would consume.
    """
    # Build a lookup from tool name -> tool object
    tool_map = {t.name: t for t in tools}

    # Track which tools have been cataloged
    cataloged = set()
    lines = []

    for category_name, tool_names in TOOL_CATEGORIES.items():
        category_lines = []
        for name in tool_names:
            if name not in tool_map:
                continue
            tool = tool_map[name]
            schema = tool.inputSchema if hasattr(tool, 'inputSchema') and tool.inputSchema else {}
            properties = schema.get("properties", {})
            required = set(schema.get("required", []))

            # Build compact signature
            params = []
            for param_name, param_info in properties.items():
                param_type = param_info.get("type", "any")
                type_map = {"integer": "int", "number": "float", "string": "str",
                            "boolean": "bool", "array": "list", "object": "dict"}
                py_type = type_map.get(param_type, param_type)

                if param_name in required:
                    params.append(f"{param_name}: {py_type}")
                else:
                    default = param_info.get("default", "None")
                    if isinstance(default, str):
                        default = f'"{default}"'
                    params.append(f"{param_name}: {py_type} = {default}")

            sig = ", ".join(params)
            # First line of description only
            desc = (tool.description or "").split('\n')[0].strip()
            if len(desc) > 80:
                desc = desc[:77] + "..."

            category_lines.append(f"  await ableton.{name}({sig})  # {desc}")
            cataloged.add(name)

        if category_lines:
            lines.append(f"\n## {category_name}")
            lines.extend(category_lines)

    # Any uncategorized tools
    uncategorized = []
    for name, tool in tool_map.items():
        if name not in cataloged:
            schema = tool.inputSchema if hasattr(tool, 'inputSchema') and tool.inputSchema else {}
            properties = schema.get("properties", {})
            required = set(schema.get("required", []))
            params = []
            for param_name, param_info in properties.items():
                param_type = param_info.get("type", "any")
                type_map = {"integer": "int", "number": "float", "string": "str",
                            "boolean": "bool", "array": "list", "object": "dict"}
                py_type = type_map.get(param_type, param_type)
                if param_name in required:
                    params.append(f"{param_name}: {py_type}")
                else:
                    params.append(f"{param_name} = None")
            sig = ", ".join(params)
            desc = (tool.description or "").split('\n')[0].strip()
            if len(desc) > 80:
                desc = desc[:77] + "..."
            uncategorized.append(f"  await ableton.{name}({sig})  # {desc}")

    if uncategorized:
        lines.append(f"\n## Other")
        lines.extend(uncategorized)

    return "\n".join(lines)
