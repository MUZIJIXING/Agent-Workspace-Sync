/* Focus Garden — 阶段 2：交互 / 阶段 3：可访问性与边界
   纯本地脚本：无外部资源、无依赖、无网络请求。
   所有任务文本一律通过 textContent 写入，不使用 innerHTML。 */
(function () {
  'use strict';

  var STORAGE_KEY = 'focus-garden.tasks.v1';
  var DONE_TEXT = '取消完成';
  var TODO_TEXT = '完成';
  var THEME_LABEL = { light: '深色', dark: '浅色' };
  var THEME_ICON = { light: '🌙', dark: '☀️' };

  var form = document.getElementById('task-form');
  var input = document.getElementById('task-input');
  var list = document.getElementById('task-list');
  var stats = document.getElementById('task-stats');
  var emptyState = document.getElementById('empty-state');
  var actionStatus = document.getElementById('action-status');
  var themeToggle = document.getElementById('theme-toggle');
  var themeLabel = document.getElementById('theme-toggle-label');
  var themeIcon = themeToggle.querySelector('.theme-toggle-icon');
  var filterButtons = Array.prototype.slice.call(
    document.querySelectorAll('.filter-btn')
  );

  var tasks = [];
  var filter = 'all';
  var nextId = 1;

  /* ---------- 数据 ---------- */

  // 首次访问（localStorage 里还没有数据）时，用页面里已有的演示任务作为种子。
  function seedFromDom() {
    var seeded = [];
    var items = list.querySelectorAll('.task-item');
    Array.prototype.forEach.call(items, function (item) {
      var textNode = item.querySelector('.task-text');
      if (!textNode) {
        return;
      }
      seeded.push({
        id: Number(item.getAttribute('data-id')),
        text: textNode.textContent.trim(),
        completed: item.getAttribute('data-completed') === 'true'
      });
    });
    return seeded;
  }

  function readStored() {
    try {
      return window.localStorage.getItem(STORAGE_KEY);
    } catch (error) {
      // localStorage 被禁用（隐私模式、策略限制）时退化为本次会话的内存态。
      return null;
    }
  }

  // 只接受符合约定结构的数据：损坏 JSON、非数组、以及脏元素都会被丢弃。
  // 返回 null 表示“数据不可用”，返回数组表示“可用的任务列表（可能为空）”。
  function sanitizeTasks(raw) {
    if (!Array.isArray(raw)) {
      return null;
    }
    var seenIds = [];
    var cleaned = [];
    raw.forEach(function (entry) {
      if (!entry || typeof entry !== 'object') {
        return;
      }
      var id = Number(entry.id);
      var text = typeof entry.text === 'string' ? entry.text.trim() : '';
      if (!Number.isInteger(id) || id < 1 || text === '' ||
          seenIds.indexOf(id) !== -1) {
        return;
      }
      seenIds.push(id);
      cleaned.push({ id: id, text: text, completed: entry.completed === true });
    });
    return cleaned;
  }

  function loadTasks() {
    var stored = readStored();
    if (stored === null) {
      // 首次访问（localStorage 里还没有数据）时，用页面里已有的演示任务作为种子。
      return seedFromDom();
    }

    var parsed = null;
    try {
      parsed = JSON.parse(stored);
    } catch (error) {
      parsed = null;
    }

    var cleaned = sanitizeTasks(parsed);
    if (cleaned === null) {
      // 数据损坏或不是数组：不猜测内容，从空列表重新开始，
      // init() 结束前会把干净的 [] 写回，避免下次再读到坏数据。
      return [];
    }
    return cleaned;
  }

  function saveTasks() {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(tasks));
    } catch (error) {
      // 写入失败（被禁用、配额已满）不应打断页面交互。
    }
  }

  function maxId() {
    return tasks.reduce(function (max, task) {
      var id = Number(task.id);
      return Number.isFinite(id) && id > max ? id : max;
    }, 0);
  }

  /* ---------- 渲染 ---------- */

  function createItem(task) {
    var li = document.createElement('li');
    li.className = 'task-item';
    li.setAttribute('data-id', String(task.id));
    li.setAttribute('data-completed', task.completed ? 'true' : 'false');

    var check = document.createElement('span');
    check.className = 'task-check';
    check.setAttribute('aria-hidden', 'true');
    check.textContent = task.completed ? '✓' : '';

    var text = document.createElement('span');
    text.className = 'task-text';
    text.id = 'task-text-' + task.id;
    text.textContent = task.text;

    var actions = document.createElement('span');
    actions.className = 'task-actions';

    var toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'task-toggle';
    toggle.setAttribute('data-id', String(task.id));
    toggle.setAttribute(
      'aria-label',
      (task.completed ? '取消完成：' : '标记完成：') + task.text
    );
    toggle.textContent = task.completed ? DONE_TEXT : TODO_TEXT;

    var remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'task-delete';
    remove.setAttribute('data-id', String(task.id));
    remove.setAttribute('aria-label', '删除任务：' + task.text);
    remove.textContent = '删除';

    actions.appendChild(toggle);
    actions.appendChild(remove);
    li.appendChild(check);
    li.appendChild(text);
    li.appendChild(actions);
    return li;
  }

  function visibleTasks() {
    if (filter === 'active') {
      return tasks.filter(function (task) {
        return !task.completed;
      });
    }
    if (filter === 'completed') {
      return tasks.filter(function (task) {
        return task.completed;
      });
    }
    return tasks.slice();
  }

  function renderStats() {
    var done = tasks.filter(function (task) {
      return task.completed;
    }).length;
    stats.textContent =
      '共 ' + tasks.length + ' 项任务，' + done + ' 项已完成，' +
      (tasks.length - done) + ' 项进行中';
  }

  function renderFilters() {
    filterButtons.forEach(function (button) {
      var active = button.getAttribute('data-filter') === filter;
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
  }

  function render() {
    var visible = visibleTasks();

    list.textContent = '';
    visible.forEach(function (task) {
      list.appendChild(createItem(task));
    });

    emptyState.hidden = visible.length > 0;
    renderStats();
    renderFilters();
  }

  /* ---------- 播报与焦点 ---------- */

  // 把操作结果写进 aria-live 区域，供屏幕阅读器播报。
  function announce(message) {
    if (!actionStatus) {
      return;
    }
    actionStatus.textContent = message;
  }

  function focusTaskButton(id, className) {
    var buttons = list.querySelectorAll('.' + className);
    var i;
    for (i = 0; i < buttons.length; i += 1) {
      if (Number(buttons[i].getAttribute('data-id')) === id) {
        buttons[i].focus();
        return true;
      }
    }
    return false;
  }

  // 目标已不在当前视图（被删除，或筛选下被移出）时，把焦点交给相邻任务。
  function focusNearestToggle(position) {
    var toggles = list.querySelectorAll('.task-toggle');
    if (toggles.length === 0) {
      input.focus();
      return;
    }
    var index = position < 0 ? 0 : Math.min(position, toggles.length - 1);
    toggles[index].focus();
  }

  /* ---------- 行为 ---------- */

  function handleSubmit(event) {
    // 阻止浏览器原生 GET 提交（否则会刷新页面）。
    event.preventDefault();

    var value = input.value.trim();
    if (value === '') {
      // 拒绝空白任务，仅把焦点留在输入框。
      input.focus();
      return;
    }

    tasks.push({ id: nextId, text: value, completed: false });
    nextId += 1;
    input.value = '';

    // 新建的任务是未完成状态，若正停留在“已完成”筛选下会看不到它。
    if (filter === 'completed') {
      filter = 'all';
    }

    saveTasks();
    render();
    announce('已添加任务：' + value);
    input.focus();
  }

  function handleFilterClick(button) {
    var value = button.getAttribute('data-filter');
    filter = value === 'active' || value === 'completed' ? value : 'all';
    render();
  }

  function handleListClick(event) {
    var button = event.target.closest('.task-toggle, .task-delete');
    if (!button) {
      return;
    }

    var id = Number(button.getAttribute('data-id'));
    var index = tasks.findIndex(function (task) {
      return Number(task.id) === id;
    });
    if (index === -1) {
      return;
    }

    // 删除前的可见位置，用于把焦点落到“顶上来的那一条”。
    var position = visibleTasks().findIndex(function (task) {
      return Number(task.id) === id;
    });
    var text = tasks[index].text;
    var toggled = button.classList.contains('task-toggle');

    if (toggled) {
      tasks[index].completed = !tasks[index].completed;
      announce(
        (tasks[index].completed ? '已完成：' : '已取消完成：') + text
      );
    } else {
      tasks.splice(index, 1);
      announce('已删除任务：' + text);
    }

    saveTasks();
    render();

    // render() 重建整个列表会让焦点掉回文档开头：任务还在视图里就放回同一个
    // 按钮上，否则（删除，或筛选下被移出）交给相邻任务。
    if (!toggled || !focusTaskButton(id, 'task-toggle')) {
      focusNearestToggle(position);
    }
  }

  function currentTheme() {
    return document.documentElement.getAttribute('data-theme') === 'dark'
      ? 'dark'
      : 'light';
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    themeLabel.textContent = THEME_LABEL[theme];
    themeIcon.textContent = THEME_ICON[theme];
    // 按钮语义：按下 = 深色主题已启用。
    themeToggle.setAttribute(
      'aria-pressed',
      theme === 'dark' ? 'true' : 'false'
    );
  }

  function handleThemeToggle() {
    applyTheme(currentTheme() === 'dark' ? 'light' : 'dark');
  }

  /* ---------- 启动 ---------- */

  function init() {
    tasks = loadTasks();
    nextId = maxId() + 1;
    // 让按钮文案、图标与 aria-pressed 一上来就和 data-theme 保持一致。
    applyTheme(currentTheme());
    render();

    form.addEventListener('submit', handleSubmit);
    list.addEventListener('click', handleListClick);
    themeToggle.addEventListener('click', handleThemeToggle);
    filterButtons.forEach(function (button) {
      button.addEventListener('click', function () {
        handleFilterClick(button);
      });
    });

    // 落盘：首次访问写入种子数据；读到损坏数据时用干净的列表覆盖它。
    saveTasks();
  }

  init();
})();
