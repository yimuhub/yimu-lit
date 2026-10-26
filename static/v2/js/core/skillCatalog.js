/* 技能清单缓存：把 /api/skills/tree 摊平成岗位绑定用的 {name,title,medium,genre} 列表。

   设置页的「模型与岗位」矩阵与右栏协作面板都要显示技能中文标题，共用这一份缓存，
   避免同一个接口在两处各拉一次。失败返回空数组，调用方回落到显示 slug。 */

import { api } from './api.js';

export const SKILL_TITLES = {};

let catalogPromise = null;

export function loadSkillCatalog() {
  if (!catalogPromise) {
    catalogPromise = api
      .skillkitTree('')
      .then((res) => {
        const flat = [];
        for (const node of (res && res.tree) || []) {
          for (const g of node.groups || []) {
            for (const s of g.skills || []) {
              flat.push({ name: s.name, title: s.title || s.name, medium: node.label || '', genre: g.label || '' });
              SKILL_TITLES[s.name] = s.title || s.name;
            }
          }
        }
        return flat;
      })
      .catch(() => []);
  }
  return catalogPromise;
}
