export const ru = {
  mrp: {
    title: 'Результаты прогона MRP №{runId}',
    run: 'RUN',
    status: 'Статус',
    startedAt: 'Старт',
    finishedAt: 'Финиш',
    horizonDays: 'Горизонт (дней)',
    weeklyYes: 'Да',
    weeklyNo: 'Нет',
    weeklyLabel: 'Недельный режим',
    tabs: {
      production: 'Заказы на производство',
      purchases: 'Заказы на закупку',
      productionDetail: 'Производство (детально)',
      purchasesDetail: 'Закупки (детально)',
      capacity: 'Мощности',
      pegging: 'Pegging',
      components: 'Компоненты заказа'
    },
    sections: {
      detail: 'Детальный анализ'
    },
    errors: {
      loadFailed: 'Ошибка загрузки результатов',
      shortageReportFailed: 'Не удалось сформировать отчет о дефиците.'
    },
    summary: {
      productionOrders: 'Производственные заказы',
      purchaseRequests: 'Заявки на закупку',
      overloadedBuckets: 'Перегруженные бакеты',
      overloadTotal: 'Суммарный перегруз (ч)',
      warnings: {
        title: 'Предупреждения',
        caption: 'Нажмите, чтобы развернуть'
      },
      kindIssues: {
        button: 'Проблемы привязки видов'
      }
    },
    columns: {
      name: 'Наименование',
      article: 'Артикул',
      qty: 'Количество',
      normPerUnit: 'Норма, ч/шт',
      normTotal: 'Норматив всего, ч',
      orderId: 'Заказ',
      purchaseId: 'Закупка',
      itemId: 'Номенклатура (ID)',
      needDate: 'Требуемая дата',
      startDate: 'Старт',
      finishDate: 'Финиш',
      bucketType: 'Бакет',
      bucketDate: 'Дата бакета',
      priorityIndex: 'Приоритет',
      stages: 'Стадии',
      areaId: 'Участок',
      hoursPlanned: 'Запланировано (ч)',
      hoursAvailable: 'Доступно (ч)',
      overloadHours: 'Перегруз (ч)',
      orderDate: 'Дата заказа',
      leadTimeDays: 'Срок поставки (дн)'
    },
    group: {
      productionKind: 'Вид производства:',
      ordersCount: 'Заказов',
      normSumHours: 'Норматив всего',
      capOverloadHours: 'Перегруз',
      urgencyDays: 'Срочн.'
    },
    filters: {
      fromDate: 'От даты (YYYY-MM-DD)',
      toDate: 'До даты (YYYY-MM-DD)',
      apply: 'Применить',
      reset: 'Сбросить фильтры',
      bucketOption: {
        any: 'Любой',
        daily: 'daily',
        weekly: 'weekly'
      }
    },
    actions: {
      csv: 'CSV',
      xlsx: 'XLSX',
      shortageReport: 'Отчет о дефиците',
      refresh: 'Обновить',
      show: 'Показать',
      showByOrder: 'Показать состав (по заказу)'
    },
    badge: {
      noNormPerUnit: 'без норматива',
      overload: 'перегруз'
    },
    placeholder: {
      noArticle: '—',
      itemNameFallback: 'Номенклатура #{id}'
    },
    pegging: {
      child: 'Дочерний',
      parent: 'Родительский',
      qtyContribution: 'Вклад кол-ва',
      needDate: 'Дата потребности',
      parentNeedDate: 'Дата потребности родителя',
      filters: {
        childItemId: 'Дочерний item_id',
        parentItemId: 'Родительский item_id'
      }
    },
    kindIssues: {
      title: 'Проблемы привязки видов производства',
      columns: {
        kindId: 'Вид (ID)',
        kindName: 'Вид производства',
        item: 'Номенклатура',
        article: 'Артикул',
        rootArticle: 'Артикул корневого изделия',
        spec: 'Спецификация',
        code: 'Код'
      }
    },
    components: {
      selectOrder: 'Выберите производственный заказ',
      columns: {
        name: 'Компонент',
        requiredQty: 'Требуемое кол-во',
        stage: 'Этап'
      },
      errors: {
        loadFailed: 'Ошибка загрузки результатов',
        shortageReportFailed: 'Не удалось сформировать отчет о дефиците.'
      }
    }
  }
}