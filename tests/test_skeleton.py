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
    """_conf_schema.json 合法且覆盖总纲 §6 的骨架分组。

    补丁 XVIII 起为两层结构：preset（新手设置）在前、advanced（高级调参）
    收拢原 autonomy/decision/output_gate/sleep/capabilities/memory/model
    七组；misc 组（补丁 XVII）与 persona 组（life_extra 挪入 preset）消失。
    """
    schema = json.loads(
        (WORKDIR / "_conf_schema.json").read_text(encoding="utf-8")
    )
    assert list(schema)[:2] == ["preset", "advanced"]
    advanced = schema["advanced"]["items"]
    for group in ("decision", "output_gate", "sleep", "capabilities",
                  "memory", "model"):
        # 补丁 XVII：misc 组只含死键 log_level，已整组删除；
        # 补丁 XVIII：persona 组的 life_extra 挪入 preset，组容器消失
        assert group in advanced, f"缺少配置分组 {group}"
        assert advanced[group]["type"] == "object"
        assert isinstance(advanced[group]["items"], dict)
    decision = advanced["decision"]["items"]
    # 关键默认值抽查（总纲：默认保守）
    assert decision["daily_impulse_limit"]["default"] == 3
    assert decision["activity_probability"]["default"] == 0.8
    # M3 补丁 IV：判定零 API 消耗，心跳默认提频到 5 分钟，频率由无聊曲线控制
    assert decision["impulse_check_interval_minutes"]["default"] == 5
    assert decision["activity_probability_min"]["default"] == 0.1
    assert decision["activity_probability_ramp_minutes"]["default"] == 60
    # M3 补丁 VII：兴趣多样性配置（补丁 XVII：decay 默认 0.9 → 0.7）
    assert decision["interest_daily_decay"]["default"] == 0.7
    assert decision["recent_topic_window"]["default"] == 6
    assert decision["recent_topic_penalty"]["default"] == [0.5, 0.3, 0.15]
    assert decision["exploration_window"]["default"] == 4
    assert decision["exploration_trigger"]["default"] == 3
    assert decision["interest_cooldown_threshold"]["default"] == 0.85
    assert decision["interest_cooldown_factor"]["default"] == 0.4
    # M3 补丁 VIII：分享角色化改写
    og = advanced["output_gate"]["items"]
    assert og["share_rewrite_enabled"]["type"] == "bool"
    assert og["share_rewrite_enabled"]["default"] is True
    assert og["share_rewrite_prompt"]["type"] == "text"
    assert "{report}" in og["share_rewrite_prompt"]["default"]
    assert "{mood}" in og["share_rewrite_prompt"]["default"]
    assert og["share_max_length"]["default"] == 120
    assert decision["max_run_seconds"]["default"] == 300
    assert decision["decision_mode"]["options"] == [
        "rules", "hybrid", "llm"
    ]
    assert decision["decision_mode"]["default"] == "hybrid"
    assert advanced["capabilities"]["items"]["cooldown_between_activities_hours"]["default"] == 2.0
    assert og["daily_message_limit"]["default"] == 10
    assert advanced["sleep"]["items"]["wake_n_messages"]["default"] == 3
    assert advanced["model"]["items"]["provider_id"]["default"] == ""
    # M2-C3 / 补丁 XVIII：life_extra 在新手组，默认模板要存在且像样
    life_extra = schema["preset"]["items"]["life_extra"]
    assert life_extra["type"] == "text"
    assert "生活背景参考" in life_extra["default"]
    assert "生活补充设定" not in life_extra["default"]
    assert "作息习惯" in life_extra["default"]
    # M3：休眠与 agent 循环配置项
    sleep_items = advanced["sleep"]["items"]
    assert sleep_items["sleep_mute_replies"]["type"] == "bool"
    assert sleep_items["sleep_mute_replies"]["default"] is True
    assert sleep_items["wake_source"]["options"] == ["all", "owner_only"]
    assert sleep_items["dream_probability"]["default"] == 0.3
    assert "owner_id" in sleep_items
    # M3 补丁 II：清醒待机与确认/告别消息
    assert sleep_items["awake_standby_minutes"]["default"] == 30
    assert sleep_items["wake_ack_message"]["default"] == "醒了，怎么了？"
    assert sleep_items["sleep_farewell_message"]["default"] == ""
    assert decision["agent_activities"]["type"] == "list"
    assert decision["agent_activities"]["default"] == [
        "surf", "read", "game"
    ]
    assert decision["single_run_token_budget"]["default"] == 20000
    assert decision["max_tool_rounds"]["default"] == 8


def test_main_py_compiles():
    """main.py 语法可编译（真实加载验收需 AstrBot 重启，见交付报告）。"""
    py_compile.compile(str(WORKDIR / "main.py"), doraise=True)


def test_all_core_modules_compiles():
    for path in (WORKDIR / "core").glob("*.py"):
        py_compile.compile(str(path), doraise=True)
