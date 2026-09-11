from mcp_manager.imports import normalize_import


def test_cordis_javascript_on_unrelated_plugin_does_not_block_mcp_import():
    result = normalize_import("""
- id: workspace
  config:
    root: !!js process.cwd()
- target: agent
  insert:
    - name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: example
        transport: stdio
        command: node
        args: [server.js]
""", "dsh")
    assert result["errors"] == []
    assert result["items"][0]["name"] == "example"


def test_cordis_javascript_inside_mcp_reports_item_and_keeps_other_entries():
    result = normalize_import("""
- target: agent
  insert:
    - name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: scripted
        command: node
        args: !js someArbitraryFunction()
    - name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: valid
        command: uv
""", "dsh")
    assert [i["name"] for i in result["items"]] == ["valid"]
    assert result["errors"][0]["name"] == "scripted"
    assert "args" in result["errors"][0]["error"]
    assert "someArbitraryFunction" not in result["errors"][0]["error"]


def test_yaml_executable_python_tag_is_never_constructed():
    result = normalize_import('!!python/object/apply:os.system ["must-not-execute"]', "dsh")
    assert result["items"] == []
    assert result["errors"]
