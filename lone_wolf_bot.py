"""
Lone Wolf Labs bot v9 - TOUGHER DODGER
for Builders - Mech Royale (mechfight/1).

Strategy:
- Hunt the weakest, closest enemy.
- Close range aggressively and strafe while shooting.
- Use gun, laser, lightning, and mortar whenever effective.
- Take cubes and second weapons aggressively.
- Escape the zone only when genuinely necessary.
- Retreat only below 18% HP or when heavily outgunned.
- Favor health, armor, and mobility upgrades to last longer under squad pressure.
"""

from __future__ import annotations

import array
import json
import math
import os
import random
import sys
from typing import Any, Dict, List, Optional, Tuple

import mechfight as mf


def _wrap_angle(angle: float) -> float:
    while angle > math.pi:
        angle -= 2.0 * math.pi
    while angle < -math.pi:
        angle += 2.0 * math.pi
    return angle


def _dist2d(a: List[float], b: List[float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Terrain:
    def __init__(self, map_dir: str) -> None:
        self.ok = False
        try:
            base = os.path.join(map_dir, "v2")
            with open(os.path.join(base, "world.json"), "r", encoding="utf-8") as f:
                meta = json.load(f)["terrain"]
            self.min_x = float(meta["minX"])
            self.min_y = float(meta["minY"])
            self.step = float(meta["step"])
            self.nx = int(meta["nx"])
            self.ny = int(meta["ny"])
            data = array.array("h")
            with open(os.path.join(base, str(meta.get("file", "terrain.i16"))), "rb") as f:
                data.fromfile(f, self.nx * self.ny)
            if sys.byteorder == "big":
                data.byteswap()
            self.data = data
            self.ok = True
        except Exception:
            self.ok = False

    def ground(self, x: float, y: float) -> Optional[float]:
        if not self.ok:
            return None
        i = max(0, min(self.nx - 1, int(round((x - self.min_x) / self.step))))
        j = max(0, min(self.ny - 1, int(round((y - self.min_y) / self.step))))
        return self.data[j * self.nx + i] / 100.0


class Bot:
    name = "lone-wolf-v9-tougher"

    def __init__(self, hello: mf.Hello) -> None:
        cfg = hello.get("config", {})
        loot_cfg = cfg.get("loot", {})
        self.pickup_radius = float(loot_cfg.get("pickup_radius", 4.0))
        self.upgrades_cfg = cfg.get("upgrades", [])
        self.upgrade_priority = {}

        for u in self.upgrades_cfg:
            uid = str(u.get("id", ""))
            effects = u.get("effects", [])
            score = 0
            for ef in effects:
                path = str(ef.get("path", ""))
                path = path.lower()
                uid = uid.lower()
                if any(term in path for term in ("damage_taken", "damage_reduction", "resist", "armor", "max_hp", "regen", "shield")) or "armor" in uid:
                    score += 40
                elif "damage" in path or "rate" in path or "cooldown" in path:
                    score += 25
                if "speed" in path or "boost" in path:
                    score += 35
                if "energy" in path:
                    score += 18
                if "sensor" in uid or "vision" in path or "sound" in path:
                    score += 8
            self.upgrade_priority[uid] = score

        self.rng = random.Random(int(hello.get("seed", 1234567)))
        self.last_damage_t = -999.0
        self.last_damage_bearing = None
        self.laser_charging = [False, False]
        self.terrain = Terrain(str(hello.get("map", {}).get("dir", "/arena")))
        self.skip_pickups = {}
        self.loot_mem = None
        self.last_pos = None
        self.stuck_ticks = 0
        self._strafe_dir = self.rng.choice([-1.0, 1.0])
        self._strafe_until = 0.0
        self._hunt_target_id = None
        self._hunt_until = 0.0

    def _pick_upgrade(self, offer: Dict[str, Any]) -> int:
        choices = offer.get("choices", [])
        if not choices:
            return 0

        best_idx = 0
        best_score = -10**9

        for idx, ch in enumerate(choices):
            uid = str(ch.get("id", ""))
            desc = str(ch.get("desc", "")).lower()
            s = self.upgrade_priority.get(uid.lower(), 0)

            if any(term in desc for term in ("damage taken", "damage reduction", "resist", "armor", "health", " hp", "regen", "shield")) or desc.startswith("hp"):
                s += 35
            elif "damage" in desc or "rate" in desc or "cooldown" in desc:
                s += 20
            if "speed" in desc or "boost" in desc:
                s += 28
            if "energy" in desc:
                s += 15
            if "vision" in desc or "sensor" in desc or "sound" in desc:
                s += 6

            if s > best_score:
                best_score = s
                best_idx = idx

        return best_idx

    def _zone_target(self, obs: mf.Observation, x: float, y: float) -> Tuple[float, float]:
        zone = obs["zone"]
        t = float(obs["t"])
        shrink_start = float(zone.get("shrink_start", t))
        shrink_end = float(zone.get("shrink_end", t + 1.0))
        cur_c = zone.get("center", [0.0, 0.0])
        cur_r = float(zone.get("radius", 500.0))
        nxt_c = zone.get("next_center", cur_c)
        nxt_r = float(zone.get("next_radius", cur_r))

        if t >= shrink_start:
            if shrink_end > shrink_start:
                frac = max(0.0, min(1.0, (t - shrink_start) / (shrink_end - shrink_start)))
            else:
                frac = 1.0
            cx = cur_c[0] + (nxt_c[0] - cur_c[0]) * frac
            cy = cur_c[1] + (nxt_c[1] - cur_c[1]) * frac
            rad = cur_r + (nxt_r - cur_r) * frac
        else:
            cx, cy, rad = cur_c[0], cur_c[1], cur_r

        safe_r = max(5.0, rad * 0.82)
        dx = nxt_c[0] - cx
        dy = nxt_c[1] - cy
        dist = math.hypot(dx, dy)

        if dist > 1e-6:
            tx = cx + (dx / dist) * min(dist * 0.75, safe_r * 0.55)
            ty = cy + (dy / dist) * min(dist * 0.75, safe_r * 0.55)
        else:
            tx, ty = cx, cy

        my_now = math.hypot(x - cx, y - cy)
        my_next = math.hypot(x - nxt_c[0], y - nxt_c[1])

        if my_now < safe_r and my_next > nxt_r * 0.90:
            return float(nxt_c[0]), float(nxt_c[1])

        return float(tx), float(ty)

    def _fire_decision(
        self,
        slot: int,
        weapon: Optional[mf.WeaponObs],
        dist: float,
        has_target: bool,
        energy: float,
        target_has_mortar: bool,
    ) -> bool:
        if weapon is None or not has_target:
            self.laser_charging[slot] = False
            return False

        kind = weapon.get("kind")
        stats = weapon.get("stats", {})
        max_range = float(stats.get("range", 200.0))

        if kind == "gun":
            ammo = float(weapon.get("ammo", 0.0))
            reload_left = float(weapon.get("reload", 0.0))
            if reload_left > 0.0 or ammo <= 0.0:
                return False
            return dist <= max_range * 0.98

        if kind == "lightning":
            if energy < 10.0 or dist > min(50.0, max_range):
                return False
            return float(weapon.get("cooldown", 0.0)) <= 0.05

        if kind == "mortar":
            if dist < 18.0 or dist > max_range:
                return False
            return float(weapon.get("cooldown", 0.0)) <= 0.05

        if kind == "laser":
            charge_time = float(weapon.get("charge", 0.0))
            full_charge = float(stats.get("charge", 0.75))

            if dist > max_range:
                self.laser_charging[slot] = False
                return False

            target_charge = full_charge * (0.80 if target_has_mortar else 0.95)

            if not self.laser_charging[slot]:
                if float(weapon.get("cooldown", 0.0)) <= 0.05 and energy > 12.0:
                    self.laser_charging[slot] = True
                    return True
                return False

            if charge_time >= target_charge:
                self.laser_charging[slot] = False
                return False

            return True

        return False

    def _projectile_dodge(self, obs: mf.Observation, me: Dict[str, Any]) -> Tuple[float, float, float]:
        """Return a cautious lateral dodge for shots predicted to pass close."""
        pos = me.get("pos", [0.0, 0.0, 0.0])
        vel = me.get("vel", [0.0, 0.0, 0.0])
        best = (0.0, 0.0, 0.0)
        horizon = 0.85

        for projectile in obs.get("projectiles", []):
            ppos = projectile.get("pos")
            pvel = projectile.get("vel")
            if not ppos or not pvel or len(ppos) < 3 or len(pvel) < 3:
                continue

            # Relative projectile position and velocity, including vertical motion.
            rx, ry, rz = (float(ppos[i]) - float(pos[i]) for i in range(3))
            vx, vy, vz = (float(pvel[i]) - float(vel[i]) for i in range(3))
            vv = vx * vx + vy * vy + vz * vz
            if vv < 1e-6:
                continue

            hit_time = -(rx * vx + ry * vy + rz * vz) / vv
            if hit_time < 0.03 or hit_time > horizon:
                continue

            cx, cy, cz = rx + vx * hit_time, ry + vy * hit_time, rz + vz * hit_time
            closest = math.sqrt(cx * cx + cy * cy + cz * cz)
            danger_radius = 12.0 if projectile.get("kind") == "mortar" else 6.0
            if closest >= danger_radius:
                continue

            horizontal_speed = math.hypot(vx, vy)
            if horizontal_speed < 1e-3:
                continue

            # Step sideways across the path, choosing the side away from the pass.
            side_measure = vx * cy - vy * cx
            if abs(side_measure) > 1e-3:
                side = -1.0 if side_measure > 0.0 else 1.0
            else:
                side = 1.0 if int(me.get("id", 0)) % 2 == 0 else -1.0

            strength = ((danger_radius - closest) / danger_radius) * (
                0.45 + 0.55 * (1.0 - hit_time / horizon)
            )
            dx = side * (-vy / horizontal_speed)
            dy = side * (vx / horizontal_speed)
            if strength > best[2]:
                best = (dx, dy, strength)

        return best

    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        x, y, z = float(me["pos"][0]), float(me["pos"][1]), float(me["pos"][2])
        hp = float(me.get("hp", 1400.0))
        max_hp = max(1.0, float(me.get("max_hp", 1400.0)))
        hp_ratio = hp / max_hp
        energy = float(me.get("energy", 0.0))
        in_zone = bool(me.get("in_zone", True))
        t = float(obs["t"])
        my_stats = me.get("stats", {})
        muzzle_z = z + float(my_stats.get("muzzle_height", 7.0))

        for ev in obs.get("events", []):
            if ev.get("type") == "damaged":
                self.last_damage_t = t
                if ev.get("bearing") is not None:
                    self.last_damage_bearing = float(ev["bearing"])
            elif ev.get("type") == "take_failed":
                self.skip_pickups[int(ev.get("id", -1))] = t + 25.0

        if self.last_pos is not None:
            moved = math.hypot(x - self.last_pos[0], y - self.last_pos[1])
            if moved < 0.12:
                self.stuck_ticks += 1
            else:
                self.stuck_ticks = 0
        else:
            self.stuck_ticks = 0

        self.last_pos = (x, y)

        act: mf.Action = {"tick": obs["tick"]}

        if obs.get("offer"):
            act["pick"] = self._pick_upgrade(obs["offer"])  # type: ignore[arg-type]

        weapons = me.get("weapons", [None, None])
        gun_slot = weapons[0] if len(weapons) > 0 and weapons[0] and weapons[0].get("kind") == "gun" else None

        bullet_speed = 252.0
        if gun_slot is not None:
            bullet_speed = max(1.0, float(gun_slot.get("stats", {}).get("speed", 252.0)))
            ammo = float(gun_slot.get("ammo", 0.0))
            mag = float(gun_slot.get("stats", {}).get("mag", 12.0))
            reload_left = float(gun_slot.get("reload", 0.0))

            if reload_left <= 0.0 and ammo <= max(1.0, mag * 0.20):
                act["reload"] = True

        pickups = obs.get("pickups", [])

        # Grab any second weapon immediately.
        if len(weapons) > 1 and weapons[1] is None:
            for p in pickups:
                if p.get("kind") in ("laser", "lightning", "mortar"):
                    if _dist2d(p["pos"], [x, y]) <= self.pickup_radius:
                        act["take"] = p["id"]
                        break

        mechs = obs.get("mechs", [])
        enemies = [m for m in mechs if m.get("kind") == "mech"]

        zone = obs["zone"]
        zone_center = zone.get("center", [0.0, 0.0])
        zone_radius = float(zone.get("radius", 500.0))

        nearest_distance = min(
            (math.hypot(m["pos"][0] - x, m["pos"][1] - y) for m in enemies),
            default=999.0,
        )

        recently_hit = (t - self.last_damage_t) < 4.0
        critical_hp = hp_ratio < 0.18

        # Choose a savage target:
        # weakest first, then closest, then cube carrier.
        target = None

        if enemies:
            def kill_score(m: Dict[str, Any]) -> float:
                d = math.dist(m["pos"], [x, y, z])
                enemy_hp = float(m.get("hp", 1400.0))
                enemy_max_hp = max(1.0, float(m.get("max_hp", 1400.0)))
                hp_frac = enemy_hp / enemy_max_hp
                cubes = int(m.get("cubes", 0))

                # Lower is better.
                return d + hp_frac * 55.0 - cubes * 6.0

            target = min(enemies, key=kill_score)

            # Stick with a target briefly so we do not flip-flop.
            if self._hunt_target_id is None or t > self._hunt_until:
                self._hunt_target_id = target.get("id")
                self._hunt_until = t + 2.0

            for m in enemies:
                if m.get("id") == self._hunt_target_id:
                    target = m
                    break

        # Loot aggressively, even when an enemy is moderately close.
        loot_target = None
        loot_dist = 0.0

        if len(weapons) > 1 and weapons[1] is None and in_zone:
            held = {w.get("kind") for w in weapons if w}
            best_score = -1e9

            for p in pickups:
                pk = p.get("kind")

                if pk not in ("laser", "lightning", "mortar") or pk in held:
                    continue

                if self.skip_pickups.get(int(p["id"]), -1.0) > t:
                    continue

                ground = self.terrain.ground(float(p["pos"][0]), float(p["pos"][1]))

                if ground is not None and float(p["pos"][2]) - ground > 2.0:
                    continue

                d = _dist2d(p["pos"], [x, y])

                if d > 250.0:
                    continue

                value = {"laser": 55.0, "lightning": 35.0, "mortar": 15.0}[pk]
                score = value - 0.22 * d

                if nearest_distance < 45.0:
                    score -= 25.0

                if score > best_score:
                    best_score = score
                    loot_target = p
                    loot_dist = d

        if loot_target is not None:
            self.loot_mem = {
                "id": int(loot_target["id"]),
                "kind": loot_target["kind"],
                "pos": [
                    float(loot_target["pos"][0]),
                    float(loot_target["pos"][1]),
                    float(loot_target["pos"][2]),
                ],
                "seen": t,
            }
        elif self.loot_mem is not None:
            mem = self.loot_mem
            mem_dist = _dist2d(mem["pos"], [x, y])

            gone = (
                len(weapons) <= 1
                or weapons[1] is not None
                or not in_zone
                or t - mem["seen"] > 7.0
                or mem_dist < 35.0
                or self.skip_pickups.get(mem["id"], -1.0) > t
            )

            if gone:
                self.loot_mem = None
            else:
                loot_target = mem
                loot_dist = mem_dist

        if loot_target is not None and self.stuck_ticks >= 10:
            self.skip_pickups[int(loot_target["id"])] = t + 12.0
            self.loot_mem = None
            loot_target = None

        zx, zy = self._zone_target(obs, x, y)

        # Default movement: toward zone target.
        move_x = zx - x
        move_y = zy - y
        move_mode = "zone_advance"

        my_zone_distance = math.hypot(x - zone_center[0], y - zone_center[1])

        # Only escape zone when it truly matters.
        if not in_zone or my_zone_distance >= zone_radius * 0.92:
            move_x = zx - x
            move_y = zy - y
            move_mode = "zone_escape"

        elif critical_hp and enemies:
            # Retreat only when nearly dead.
            closest = min(enemies, key=lambda m: math.hypot(m["pos"][0] - x, m["pos"][1] - y))
            away_x = x - closest["pos"][0]
            away_y = y - closest["pos"][1]
            away_len = max(1e-6, math.hypot(away_x, away_y))

            move_x = (away_x / away_len) * 80.0 + (zx - x) * 0.45
            move_y = (away_y / away_len) * 80.0 + (zy - y) * 0.45
            move_mode = "critical_retreat"

        elif target is not None:
            # Hunt aggressively: close distance, strafe, keep shooting.
            tx, ty, tz = target["pos"]
            target_distance = math.hypot(tx - x, ty - y)
            to_target_yaw = math.atan2(ty - y, tx - x)

            if self.rng.random() < 0.04 or t > self._strafe_until:
                self._strafe_dir = self.rng.choice([-1.0, 1.0])
                self._strafe_until = t + self.rng.uniform(1.0, 2.5)

            strafe_yaw = to_target_yaw + self._strafe_dir * (math.pi * 0.5)

            # Aggressive desired range: get close enough to use every weapon.
            if hp_ratio > 0.55:
                desired_range = 45.0
            elif hp_ratio > 0.30:
                desired_range = 65.0
            else:
                desired_range = 90.0

            radial = 0.0

            if target_distance < desired_range * 0.70:
                radial = -0.45
            elif target_distance > desired_range * 1.20:
                radial = 0.85

            move_x = (
                (zx - x) * 0.30
                + math.cos(to_target_yaw) * radial * 60.0
                + math.cos(strafe_yaw) * 40.0
            )
            move_y = (
                (zy - y) * 0.30
                + math.sin(to_target_yaw) * radial * 60.0
                + math.sin(strafe_yaw) * 40.0
            )

            move_mode = "hunt_and_kill"

        elif loot_target is not None:
            move_x = float(loot_target["pos"][0]) - x
            move_y = float(loot_target["pos"][1]) - y
            move_mode = "loot_rush"

        # Keep v6's movement plan and add only a modest correction for a
        # projectile predicted to pass close to the mech.
        dodge_x, dodge_y, dodge_strength = self._projectile_dodge(obs, me)
        if dodge_strength > 0.0:
            move_x += dodge_x * 55.0 * dodge_strength
            move_y += dodge_y * 55.0 * dodge_strength
            if dodge_strength >= 0.45:
                move_mode = "projectile_evade"

        move_norm = math.hypot(move_x, move_y)

        if move_norm > 1e-6:
            act["move"] = [move_x / move_norm, move_y / move_norm]
        else:
            act["move"] = [0.0, 0.0]

        # Boost aggressively when hunting, escaping, or rushing loot.
        should_boost = (
            (not in_zone and energy > 15.0)
            or (my_zone_distance >= zone_radius * 0.90 and energy > 20.0)
            or (move_mode == "hunt_and_kill" and energy > 25.0 and nearest_distance > 55.0)
            or (recently_hit and critical_hp and energy > 25.0)
            or (loot_target is not None and loot_dist > 25.0 and energy > 35.0)
            or (dodge_strength > 0.70 and energy > 20.0)
        )

        act["boost"] = bool(should_boost)
        act["jump"] = bool(
            me.get("grounded", False)
            and self.rng.random() < 0.03
            and target is not None
        )

        if target is not None:
            tpos = target["pos"]
            tvel = target.get("vel", [0.0, 0.0, 0.0])
            horiz_dist = max(1.0, math.hypot(tpos[0] - x, tpos[1] - y))

            lead_t = min(1.0, horiz_dist / bullet_speed)
            predicted_x = tpos[0] + tvel[0] * lead_t
            predicted_y = tpos[1] + tvel[1] * lead_t

            target_cubes = int(target.get("cubes", 0))
            predicted_z = tpos[2] + 4.5 * (1.0 + 0.06 * min(target_cubes, 12))

            yaw = math.atan2(predicted_y - y, predicted_x - x)
            pitch = math.atan2(predicted_z - muzzle_z, horiz_dist)
            pitch = max(-0.85, min(0.85, pitch))

            act["aim"] = [yaw, pitch]

            target_weapons = target.get("weapons", [])
            target_has_mortar = "mortar" in target_weapons

            f0 = self._fire_decision(
                0,
                weapons[0] if len(weapons) > 0 else None,
                horiz_dist,
                True,
                energy,
                target_has_mortar,
            )

            f1 = self._fire_decision(
                1,
                weapons[1] if len(weapons) > 1 else None,
                horiz_dist,
                True,
                energy,
                target_has_mortar,
            )

            fire = [f0, f1]

            my_aim = me.get("aim", [yaw, pitch])
            aim_error = abs(_wrap_angle(float(my_aim[0]) - yaw))

            for i, weapon in enumerate(weapons[:2]):
                if weapon is not None and weapon.get("kind") == "laser" and self.laser_charging[i]:
                    continue

                if aim_error > 0.06:
                    fire[i] = False

            act["fire"] = fire

            mf.draw.line([x, y, muzzle_z], [predicted_x, predicted_y, predicted_z], "#ff2020")

        else:
            self.laser_charging = [False, False]

            if recently_hit and self.last_damage_bearing is not None:
                act["aim"] = [_wrap_angle(self.last_damage_bearing), 0.0]
            elif loot_target is not None:
                act["aim"] = [
                    math.atan2(float(loot_target["pos"][1]) - y, float(loot_target["pos"][0]) - x),
                    0.0,
                ]
            else:
                act["aim"] = [math.atan2(zy - y, zx - x), 0.0]

            act["fire"] = [False, False]

        mf.draw.point([zx, zy, z], "#ffcc00", 6)
        mf.draw.label(
            f"TOUGHER v9 | hp {hp:.0f}/{max_hp:.0f} | "
            f"mode {move_mode} | enemies {len(enemies)} | "
            f"nearest {nearest_distance:.0f}"
        )

        return act


mf.run(Bot)
