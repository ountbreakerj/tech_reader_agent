function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[char]);
}

// Cards and the sidebar share module.json as their chapter list.
export function renderLearningMenu(config) {
  const { menu, route, toc } = config;
  const href = id => `${route}?section=${encodeURIComponent(id)}`;
  const link = (id, title) => `<a href="${escapeHtml(href(id))}">${escapeHtml(title)}</a>`;

  function cards(entries) {
    const parts = [];
    let row = [];
    function flush() {
      if (row.length) parts.push(`<nav class="home-cards harness-module-grid" aria-label="学习章节">${row.join('')}</nav>`);
      row = [];
    }
    for (const entry of entries) {
      if (entry.g && !entry.id) {
        flush();
        parts.push(`<section class="learning-menu-group"><h2>${escapeHtml(entry.t)}</h2>${cards(entry.c)}</section>`);
        continue;
      }
      const description = entry.desc ? `<p>${escapeHtml(entry.desc)}</p>` : '';
      row.push(`<a class="home-card" href="${escapeHtml(href(entry.id))}"><h3>${escapeHtml(entry.t)}</h3>${description}<div class="go">${entry.g ? '查看章节' : '进入章节'} →</div></a>`);
    }
    flush();
    return parts.join('\n');
  }

  function panel(id, title, description, entries, parents) {
    const crumbs = ['<a href="#/">首页</a>', ...parents.map(parent => link(parent.id, parent.t)), `<span aria-current="page">${escapeHtml(title)}</span>`];
    return `<section class="harness-scope learning-menu" id="${escapeHtml(id)}" data-learning-menu${id === menu.id ? '' : ' hidden'}>
      <nav class="harness-breadcrumb" aria-label="当前位置">${crumbs.join('<span aria-hidden="true">/</span>')}</nav>
      <header class="harness-head"><h1>${escapeHtml(title)}</h1>${description ? `<p>${escapeHtml(description)}</p>` : ''}</header>
      ${cards(entries)}
    </section>`;
  }

  const entries = toc.filter(entry => entry.id !== menu.id);
  const panels = [panel(menu.id, config.title, menu.intro, entries, [])];
  function addGroups(items, parents) {
    for (const entry of items) {
      if (!entry.g) continue;
      if (entry.id) panels.push(panel(entry.id, entry.t, entry.desc, entry.c, parents));
      addGroups(entry.c, entry.id ? [...parents, entry] : parents);
    }
  }
  addGroups(entries, [{ id: menu.id, t: config.title }]);
  return panels.join('\n');
}
