/* Progressive enhancements only: no records, credentials or preferences stored. */
(() => {
  'use strict';
  const shell = document.querySelector('.ops-shell');
  const toggle = document.querySelector('[data-nav-toggle]');
  const nav = document.getElementById('primary-navigation');
  if (shell && toggle && nav) {
    const close = () => { shell.classList.remove('menu-open'); toggle.setAttribute('aria-expanded', 'false'); };
    toggle.hidden = false;
    shell.classList.add('js');
    toggle.addEventListener('click', () => {
      const open = !shell.classList.contains('menu-open');
      shell.classList.toggle('menu-open', open);
      toggle.setAttribute('aria-expanded', String(open));
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && shell.classList.contains('menu-open')) { close(); toggle.focus(); }
    });
    document.addEventListener('click', event => {
      if (!event.target.closest('.topbar')) close();
    });
    const desktop = window.matchMedia('(min-width: 1100px)');
    desktop.addEventListener('change', close);
  }
  document.querySelectorAll('table[data-mobile-list]').forEach(table => {
    const headers = [...table.querySelectorAll('thead tr:first-child th')];
    if (!headers.length || table.querySelector('thead tr:nth-child(2), [rowspan]')) return;
    const rows = [...table.querySelectorAll('tbody tr')];
    if (rows.some(row => row.cells.length !== headers.length && !row.querySelector('[colspan]'))) return;
    table.setAttribute('role', 'table');
    table.querySelectorAll('thead,tbody').forEach(group => group.setAttribute('role', 'rowgroup'));
    table.querySelectorAll('tr').forEach(row => row.setAttribute('role', 'row'));
    headers.forEach(header => header.setAttribute('role', 'columnheader'));
    rows.forEach(row => [...row.cells].forEach((cell, index) => {
      cell.setAttribute('role', 'cell');
      if (!cell.hasAttribute('colspan')) cell.dataset.label = headers[index].textContent.trim();
    }));
    table.classList.add('mobile-list');
  });
  document.querySelectorAll('.table-wrap').forEach((region, index) => {
    if (!region.hasAttribute('tabindex')) region.tabIndex = 0;
    if (!region.hasAttribute('role')) region.setAttribute('role', 'region');
    if (!region.hasAttribute('aria-label')) {
      const section = region.closest('section');
      const heading = section && section.querySelector('h2');
      region.setAttribute('aria-label', (heading ? heading.textContent.trim() : 'Data table ' + (index + 1)) + ' — scrollable');
    }
  });
})();
