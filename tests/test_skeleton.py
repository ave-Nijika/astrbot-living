"""R1 插件骨架自检。

注意：main.py 使用相对导入（.core.xxx），只能在 AstrBot 的插件包上下文中
真正加载；本测试做静态验收（元数据/配置 schema/语法编译），
完整验收步骤（AstrBot 重载看 WebUI）写在交付报告里。
"""

import json
import pathlib
import py_compile

import yaml

WORKDIR = pathlib.Path(__file__).resolve().parents[1]


def test_metadata_yaml_five_fields():
    """任务书红线：metadata.yaml 参照 LivingMemory 的极简五字段。"""
    meta = yaml.safe_load((WORKDIR / "metadata.yaml").read_text(encoding="utf-8"))
    for field in ("name", "author", "version", "description", "repo"):
        assert field in meta, f"metadata.yaml 缺少字段 {field}"
    assert meta["name"] == "astrbot_plugin_living"


def test_conf_schema_valid_and_groups_complete():
    """_conf_schema.json 合法且覆盖总纲 §6 的骨架分组。"""
    schema = json.loads(
        (WORKDIR / "_conf_schema.json").read_text(encoding="utf-8")
    )
    for group in ("decision", "output_gate", "sleep", "capabilities",
                  "memory", "model", "misc", "persona"):
        assert group in schema, f"缺少配置分组 {group}"
        assert schema[group]["type"] == "object"
        assert isinstance(schema[group]["items"], dict)
    # 关键默认值抽查（总纲：默认保守）
    assert schema["decision"]["items"]["daily_impulse_limit"]["default"] == 3
    assert schema["decision"]["items"]["activity_probability"]["default"] == 0.8
    assert schema["decision"]["items"]["impulse_check_interval_minutes"]["default"] == 45
    assert schema["decision"]["items"]["max_run_seconds"]["default"] == 300
    assert schema["decision"]["items"]["decision_mode"]["options"] == [
        "rules", "hybrid", "llm"
    ]
    assert schema["decision"]["items"]["decision_mode"]["default"] == "hybrid"
    assert schema["capabilities"]["items"]["cooldown_between_activities_hours"]["default"] == 2.0
    assert schema["output_gate"]["items"]["daily_message_limit"]["default"] == 10
    assert schema["sleep"]["items"]["wake_n_messages"]["default"] == 3
    assert schema["model"]["items"]["provider_id"]["default"] == ""
    # M2-C3：life_extra 默认模板要存在且像样
    life_extra = schema["persona"]["items"]["life_extra"]
    assert life_extra["type"] == "text"
    assert "生活补充设定" in life_extra["default"]
    assert "作息习惯" in life_extra["default"]
    # M3：休眠与 agent 循环配置项
    sleep_items = schema["sleep"]["items"]
    assert sleep_items["sleep_mute_replies"]["type"] == "bool"
    assert sleep_items["sleep_mute_replies"]["default"] is True
    assert sleep_items["wake_source"]["options"] == ["all", "owner_only"]
    assert sleep_items["dream_probability"]["default"] == 0.3
    assert "owner_id" in sleep_items
    assert schema["decision"]["items"]["agent_activities"]["type"] == "list"
    assert schema["decision"]["items"]["agent_activities"]["default"] == [
        "surf", "read", "game"
    ]
    assert schema["decision"]["items"]["single_run_token_budget"]["default"] == 20000
    assert schema["decision"]["items"]["max_tool_rounds"]["default"] == 8


def test_main_py_compiles():
    """main.py 语法可编译（真实加载验收需 AstrBot 重启，见交付报告）。"""
    py_compile.compile(str(WORKDIR / "main.py"), doraise=True)


def test_all_core_modules_compiles():
    for path in (WORKDIR / "core").glob("*.py"):
        py_compile.compile(str(path), doraise=True)
