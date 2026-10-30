# -*- coding: utf-8 -*-
"""岗位注册一致性门禁（v4.15.3）。

medium.MEDIUMS 派生的 MEDIUM_AGENTS_LABELS 与 agents.AGENTS / DEFAULT_AGENT_SKILLS
是三处手写注册点，历史上新增岗位时漏同步导致协作面板/技能注入缺项。
本文件把三者的双向一致写成门禁，新增文体或岗位时跑不过即说明漏改。
"""


def test_medium_agents_exist_in_agent_registry():
    """每个文体注册的岗位必须存在于 agents.AGENTS 主注册表。"""
    from bot import agents, medium

    for med, conf in medium.MEDIUMS.items():
        for name in conf.get("agents", []):
            assert name in agents.AGENTS, f"{med} 注册的岗位 {name} 不在 agents.AGENTS 中"


def test_medium_agents_have_default_skills():
    """每个文体注册的岗位必须有默认技能绑定（DEFAULT_AGENT_SKILLS），且技能真实存在。"""
    from bot import agents, medium, skillkit

    skillkit.reload()
    known = {sf.name for sf in skillkit.index()}
    for med, conf in medium.MEDIUMS.items():
        for name in conf.get("agents", []):
            skills = agents.DEFAULT_AGENT_SKILLS.get(name)
            assert skills, f"岗位 {name}（{med}）在 DEFAULT_AGENT_SKILLS 里没有默认技能绑定"
            for sk in skills:
                assert sk in known, f"岗位 {name} 绑定的技能 {sk} 在 skillkit 中不存在"


def test_default_agent_skills_reference_known_agents():
    """DEFAULT_AGENT_SKILLS 里的岗位必须是内置 AGENTS 或用户自定义岗位允许范围。"""
    from bot import agents

    for name in agents.DEFAULT_AGENT_SKILLS:
        assert name in agents.AGENTS, (
            f"DEFAULT_AGENT_SKILLS 有岗位 {name} 但 agents.AGENTS 未注册（MEDIUM_AGENTS_LABELS 派生遗漏？）"
        )
