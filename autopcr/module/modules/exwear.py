from typing import Dict, List, Optional, Tuple
from decimal import Decimal

from ..modulebase import *
from ..config import *
from ...core.pcrclient import pcrclient
from ...model.error import *
from ...db.database import db
from ...model.common import ChangeRarityUnit, ExtraEquipChangeSlot, ExtraEquipChangeUnit
from ...model.custom import UnitAttribute
from ...model.enums import eClanSupportMemberType

ATTR_NAME = {
    "hp": "生命",
    "atk": "攻击",
    "magic_str": "魔攻",
    "def_": "物防",
    "magic_def": "魔防",
    "physical_critical": "物爆",
    "magic_critical": "法爆",
    "wave_hp_recovery": "HP回复",
    "wave_energy_recovery": "TP回复",
    "dodge": "闪避",
    "physical_penetrate": "物贯",
    "magic_penetrate": "法贯",
    "life_steal": "吸血",
    "hp_recovery_rate": "HP回复率",
    "energy_recovery_rate": "TP上升",
    "energy_reduce_rate": "TP减轻",
    "accuracy": "命中",
}


def format_ex_equip_attr(attr: UnitAttribute) -> str:
    parts = []
    for param, field_name in UnitAttribute.index2name.items():
        val = getattr(attr, field_name, Decimal(0))
        if val:
            parts.append(f"{ATTR_NAME.get(field_name, field_name)}+{int(val)}")
    return '/'.join(parts) if parts else '无属性'


# support_unit/change_setting 的 support_type 取值沿用现有"撤下会战助战"的实现(会战=1)，
# 地下城与好友暂无在用样例，如测试发现设置失败，调整此映射即可
SUPPORT_TYPE_API = {
    '会战': 1,
    '好友': 2,
    '地下城': 3,
}
SUPPORT_POSITIONS = {
    '地下城': [eClanSupportMemberType.DUNGEON_SUPPORT_UNIT_1, eClanSupportMemberType.DUNGEON_SUPPORT_UNIT_2],
    '会战': [eClanSupportMemberType.CLAN_BATTLE_SUPPORT_UNIT_1, eClanSupportMemberType.CLAN_BATTLE_SUPPORT_UNIT_2],
    '好友': [1, 2],
}


@name('一键穿ex')
@default(False)
@unitchoice('ex_wear_unit_id', '角色')
@texttype('ex_wear_selection', '选择(试穿/数字如 1 1 2, 留空为查询)', '')
@description('查询角色可用普通EX装备并按编号穿戴；选择填"试穿"仅模拟不实际穿戴。')
class ex_equip_wear(Module):
    '''
    查询模式: 列出该角色三个普通EX槽位的候选装备(按彩装优先、战力降序编号)
    穿戴模式: 按槽位编号穿戴，若目标装备在他人身上则取下/互换
    '''

    SLOT_CNT = 3
    QUERY_ROW_LIMIT = 20

    @staticmethod
    def decorate(client: pcrclient, ex, unit_attr: UnitAttribute, coefficient) -> Dict:
        ex_id = ex.ex_equipment_id
        star = db.get_ex_equip_star_from_pt(ex_id, ex.enhancement_pt)
        # 会战EX装与部分装备没有炼成属性表，查询时按无炼成处理
        sub = ex.sub_status if (ex.sub_status and ex_id in db.ex_equipment_sub_status_group) else None
        attr = db.ex_equipment_data[ex_id].get_unit_attribute(star, sub)
        bonus = unit_attr.ex_equipment_mul(attr).ceil()
        power = int(bonus.get_power(coefficient) + 0.5)
        sub_str = db.get_ex_equip_sub_status_str(ex_id, sub) if sub else '空'
        label = db.get_ex_equip_name(ex_id)
        return {
            'serial_id': ex.serial_id,
            'ex_id': ex_id,
            'star': star,
            'rarity': db.get_ex_equip_rarity(ex_id),
            'power': power,
            'protected': bool(ex.protection_flag),
            'label': f"{label}★{star} {format_ex_equip_attr(attr)}" + (f" 炼成:{sub_str}" if sub_str != '空' else ''),
        }

    def describe_slot(self, client: pcrclient, unit_id: int, slot_no: int, cands: List[Dict], chosen: Optional[Dict]) -> str:
        if chosen is None:
            return f"槽位{slot_no}: 保持不变"
        if chosen['serial_id'] in [es.serial_id for es in client.data.unit[unit_id].ex_equip_slot if es.slot == slot_no]:
            return f"槽位{slot_no}: {chosen['label']} (已穿戴)"
        return f"槽位{slot_no}: {chosen['label']} 战力+{chosen['power']}"

    async def do_task(self, client: pcrclient):
        unit_id = int(self.get_config('ex_wear_unit_id') or 0)
        selection = str(self.get_config('ex_wear_selection') or '').strip()
        if not unit_id:
            raise AbortError("未指定角色")
        if unit_id not in client.data.unit:
            raise AbortError(f"未拥有角色 {db.get_unit_name(unit_id)}")
        unit = client.data.unit[unit_id]

        slot_data = db.unit_ex_equipment_slot[unit_id]
        categories = [slot_data.slot_category_1, slot_data.slot_category_2, slot_data.slot_category_3]

        position: Dict[int, Tuple[int, int]] = {}
        for uid, u in client.data.unit.items():
            for es in u.ex_equip_slot:
                if es.serial_id:
                    position[es.serial_id] = (uid, es.slot)
        cur_wear = {es.slot: es.serial_id for es in unit.ex_equip_slot}

        coefficient = db.unit_status_coefficient[1]
        read_story = set(client.data.read_story_ids)
        unit_attr = db.calc_unit_attribute(unit, read_story, client.data.ex_equips, exclude_ex_equip=True)

        per_slot: List[List[Dict]] = []
        filtered_flags: List[bool] = []
        for cat in categories:
            cands = [self.decorate(client, ex, unit_attr, coefficient)
                     for ex in client.data.ex_equips.values()
                     if not db.ex_equipment_data[ex.ex_equipment_id].clan_battle_equip_flag
                     and db.ex_equipment_data[ex.ex_equipment_id].category == cat]
            cands.sort(key=lambda d: (0 if d['rarity'] == 5 else 1, -d['power']))
            filtered = False
            if len(cands) > self.QUERY_ROW_LIMIT:
                non_gold = [d for d in cands if d['rarity'] != 3]
                if len(non_gold) < len(cands):
                    cands = non_gold
                    filtered = True
            per_slot.append(cands)
            filtered_flags.append(filtered)

        if not selection:
            self._log(self.build_query_log(client, unit_id, cur_wear, per_slot, filtered_flags, unit_attr, coefficient))
            return

        dry_run = False
        tokens = selection.split()
        if tokens and tokens[0] == '试穿':
            dry_run = True
            tokens = tokens[1:]
        if len(tokens) != self.SLOT_CNT:
            raise AbortError(f"选择格式应为{self.SLOT_CNT}个编号(不足的槽位用0表示保持不变)，试穿需带编号，如: 试穿 1 1 15")

        picked: List[Optional[Dict]] = []
        for slot_no, token in zip(range(1, self.SLOT_CNT + 1), tokens):
            try:
                n = int(token)
            except ValueError:
                raise AbortError(f"槽位{slot_no}的选择【{token}】不是编号，应为 试穿 或 数字")
            if n == 0:
                picked.append(None)
                continue
            if not 1 <= n <= len(per_slot[slot_no - 1]):
                raise AbortError(f"槽位{slot_no}编号超出范围 1~{len(per_slot[slot_no - 1])}")
            d = per_slot[slot_no - 1][n - 1]
            if d['protected'] and cur_wear.get(slot_no) != d['serial_id']:
                raise AbortError(f"槽位{slot_no}的目标装备 {d['label']} 已锁定，请先在游戏中解锁")
            picked.append(d)

        result_lines = [self.describe_slot(client, unit_id, slot_no, per_slot[slot_no - 1], d)
                        for slot_no, d in zip(range(1, self.SLOT_CNT + 1), picked)]

        actions = []
        for slot_no, d in zip(range(1, self.SLOT_CNT + 1), picked):
            if d is None:
                continue
            sid = d['serial_id']
            if cur_wear.get(slot_no) == sid:
                continue
            holder = position.get(sid)
            my_cur_sid = cur_wear.get(slot_no, 0)
            if holder:
                w_uid, w_slot = holder
                w_name = "自己" if w_uid == unit_id else db.get_unit_name(w_uid)
                if my_cur_sid:
                    actions.append((w_uid, w_slot, my_cur_sid))
                    result_lines.append(f"与{w_name}槽{w_slot}互换装备")
                else:
                    actions.append((w_uid, w_slot, 0))
                    result_lines.append(f"从{w_name}的槽{w_slot}取下{d['label']}")
            actions.append((unit_id, slot_no, sid))

        if not actions:
            result_lines.append("无需变更")
        elif not dry_run:
            for w_uid, w_slot, sid in actions:
                change = ExtraEquipChangeSlot(slot=w_slot, serial_id=sid)
                await client.unit_equip_ex([ExtraEquipChangeUnit(unit_id=w_uid, ex_equip_slot=[change], cb_ex_equip_slot=None)])

        result_lines.append("试穿完成，未实际穿戴，确认无误请去掉试穿重新发送" if dry_run
                            else f"已为{db.get_unit_name(unit_id)}装备完成！")
        self._log('\n'.join(result_lines))

    def build_query_log(self, client: pcrclient, unit_id: int, cur_wear: Dict[int, int], per_slot: List[List[Dict]], filtered_flags: List[bool], unit_attr: UnitAttribute, coefficient) -> str:
        unit_name = db.get_unit_name(unit_id)
        lines = [f"----{unit_name} EX装备攻略----", "", "【当前装备】"]
        for slot_no in range(1, self.SLOT_CNT + 1):
            sid = cur_wear.get(slot_no, 0)
            if sid and sid in client.data.ex_equips:
                d = self.decorate(client, client.data.ex_equips[sid], unit_attr, coefficient)
                lines.append(f"槽位{slot_no}: {d['label']} 战力+{d['power']}")
            else:
                lines.append(f"槽位{slot_no}: 未装备")
        lines.append("")
        for slot_no, (cands, filtered) in enumerate(zip(per_slot, filtered_flags), start=1):
            note = "，金装较多已省略" if filtered else ""
            lines.append(f"【槽位{slot_no}】(共{len(cands)}可选{note}):")
            for i, d in enumerate(cands, start=1):
                marks = []
                if cur_wear.get(slot_no) == d['serial_id']:
                    marks.append("已穿戴")
                if d['protected']:
                    marks.append("已锁定")
                mark = f" [{','.join(marks)}]" if marks else ""
                lines.append(f"{i}. {d['label']} 战力+{d['power']}{mark}")
            lines.append("")
        lines.append(f"回复 #一键穿ex {unit_name} 试穿 1 1 15 可预览效果(不实际穿戴)")
        lines.append(f"回复 #一键穿ex {unit_name} 1 1 15 按各槽位编号穿戴(0为保持不变)")
        return '\n'.join(lines)


@name('设置支援')
@default(False)
@singlechoice('support_unit_set_type', '支援类型', '会战', ['地下城', '会战', '好友'])
@texttype('support_unit_set_units', '角色列表(unit_id:星级,星级0为不指定)', '')
@description('把指定角色设置为支援出借。星级可选3/4/5，通过临时调整角色战力星级实现，完成后自动还原。')
class set_support_unit(Module):
    '''
    support_unit_set_units 形如 "1001:3,1002:0"（unit_id为角色基础id*100+1，星级0表示不指定）
    '''

    async def do_task(self, client: pcrclient):
        type_name = self.get_config('support_unit_set_type')
        units_text = str(self.get_config('support_unit_set_units') or '').strip()
        if type_name not in SUPPORT_TYPE_API:
            raise AbortError(f"未知支援类型{type_name}")
        if not units_text:
            raise AbortError("未指定角色")

        units: List[Tuple[int, int]] = []
        for token in units_text.split(','):
            token = token.strip()
            if not token:
                continue
            try:
                uid_s, star_s = token.split(':')
                unit_id, star = int(uid_s), int(star_s)
            except ValueError:
                raise AbortError(f"角色配置【{token}】格式错误，应为 unit_id:星级")
            if unit_id not in client.data.unit:
                raise AbortError(f"未拥有角色 {db.get_unit_name(unit_id)}")
            if star and star not in (3, 4, 5):
                raise AbortError(f"角色 {db.get_unit_name(unit_id)} 的星级 {star} 不合法，仅支持3/4/5")
            units.append((unit_id, star))
        if not units:
            raise AbortError("未指定角色")
        if len(units) > 2:
            raise AbortError("最多同时设置2个支援角色")

        info = await client.support_unit_get_setting()
        if type_name == '好友':
            current = {s.position: s for s in (info.friend_support_units or [])}
        else:
            current = {s.position: s for s in (info.clan_support_units or [])
                       if s.position in SUPPORT_POSITIONS[type_name]}
        candidates = [p for p in SUPPORT_POSITIONS[type_name] if p in current]
        free = [p for p in SUPPORT_POSITIONS[type_name] if p not in current]
        positions = candidates + [p for p in free if p not in candidates]
        if len(units) > len(positions):
            raise AbortError(f"{type_name}支援最多可设置{len(positions)}个角色")

        api_type = SUPPORT_TYPE_API[type_name]
        slot_nums = {pos: i + 1 for i, pos in enumerate(SUPPORT_POSITIONS[type_name])}
        logs = []
        for (unit_id, star), pos in zip(units, positions):
            old = current.get(pos)
            if old and old.unit_id == unit_id:
                logs.append(f"{db.get_unit_name(unit_id)} 已是{type_name}支援{slot_nums[pos]}号位，跳过")
                continue
            if old:
                await client.support_unit_change_setting(api_type, pos, 2, old.unit_id)
            origin_rarity = client.data.unit[unit_id].battle_rarity or client.data.unit[unit_id].unit_rarity
            try:
                if star and star != origin_rarity:
                    await client.unit_change_rarity([ChangeRarityUnit(unit_id=unit_id, battle_rarity=star)])
                await client.support_unit_change_setting(api_type, pos, 1, unit_id)
            finally:
                if star and star != origin_rarity:
                    await client.unit_change_rarity([ChangeRarityUnit(unit_id=unit_id, battle_rarity=origin_rarity)])
            logs.append(f"已将{db.get_unit_name(unit_id)}设为{type_name}支援{slot_nums[pos]}号位" + (f"(星级{star})" if star else ""))
        self._log('\n'.join(logs))
