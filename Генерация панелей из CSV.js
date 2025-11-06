"use strict";

//======================== КОНСТАНТЫ И НАСТРОЙКИ ========================
const DIMENSIONS = {
    WIDTH: 0,   // Значение по умолчанию, можно скорректировать при необходимости
    HEIGHT: 0,
    THICKNESS: 18  // Можно задать логику определения толщины, если требуется
};

const POSITION = {
    X: 0,   // Начальная позиция по X
    Y: 0,   // Начальная позиция по Y
    Z: 0    // Позиция по Z, если нужна
};

//======================== СЕРВИСНЫЕ ФУНКЦИИ ========================
const PanelBuilder = {
    createPanel: (pos, dims) => {
        const endX = pos.X + dims.WIDTH;
        const endY = pos.Y + dims.HEIGHT;
        return AddFrontPanel(pos.X, pos.Y, endX, endY, pos.Z);
    },

    configurePanel: (panel, config) => {
        panel.Name = config.NAME;
        panel.MaterialName = `${config.MATERIAL_DATA.NAME}\r${config.MATERIAL_DATA.ARTICLE}`;
        panel.Thickness = DIMENSIONS.THICKNESS;
        panel.UserProperty[config.USER_PROPERTY.NAME] = config.USER_PROPERTY.VALUE;
    }
};

//======================== ЧТЕНИЕ CSV ФАЙЛА ========================
try {
    // Диалог выбора CSV-файла
    const content = system.askReadTextFile('csv');
    if (!content) {
        throw new Error("Файл не выбран или пуст");
    }

    // Разбиение на строки
    const lines = content.split(/\r?\n/).filter(line => line.trim() !== "");
    // Первая строка — заголовок
    const header = lines.shift().split(';').map(item => item.trim());

    // Для каждой строки создаём панель
    lines.forEach((line, index) => {
        const fields = line.split(';').map(item => item.trim());
        if (fields.length < 5) {
            alert(`Строка ${index + 2} имеет недостаточное число полей.`);
            return;
        }

        // Создаём объект конфигурации для панели
        const panelConfig = {
            NAME: fields[0],
            MATERIAL_DATA: {
                NAME: fields[1],
                ARTICLE: fields[2]
            },
            USER_PROPERTY: {
                NAME: fields[3],
                VALUE: fields[4]
            }
        };

        // Можно задать позиционирование панелей, например, сдвигая их по оси X
        const panelPosition = {
            X: POSITION.X,
            Y: POSITION.Y,
            Z: POSITION.Z
        };

        // Создание и настройка панели
        try {
            const panel = PanelBuilder.createPanel(panelPosition, DIMENSIONS);
            PanelBuilder.configurePanel(panel, panelConfig);
            panel.Build();
        } catch(error) {
           alert(`Ошибка при создании панели "${panelConfig.NAME}": ${error.message}`);
        }
    });

    // Финализация изменений после создания всех панелей
    Action.Commit("Cоздания панелей из CSV");

} catch(error) {
    alert(`Ошибка: ${error.message}`);
    Action.Cancel();
}
