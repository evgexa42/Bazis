// Скрипт для создания панелей на основе размеров из списка и фрагмента Bazis
// Путь к фрагменту можно изменить ниже. Формат списка: ширина высота количество

const fragmentPath = 'C:\\Users\\жщшо\\Documents\\Bazis\\#_Фрагменты\\Фрагменты элементов мебели\\Fasad_R.fr3d';

// Описание панелей: width (X), height (Y), count
const items = [
    { width: 264, height: 542, count: 1 },
    { width: 600, height: 1800, count: 2 },
];

const gap = 100;     // Зазор между панелями по X (можно изменить при необходимости)
let currentX = 0;    // Текущая координата X для размещения следующей панели

const fragment = OpenFurniture(fragmentPath);
if (!fragment) {
    system.alert('Не удалось открыть фрагмент: ' + fragmentPath);
    halt;
}

function placePanel(width, height, offsetX) {
    const obj = fragment.Make();
    if (!obj) {
        return;
    }

    try { obj.Owner = Model.Temp; } catch (e) {}
    try { obj.Build(); } catch (e) {}

    try {
        obj.ElasticResize({ x: width, y: height, z: 0 });
    } catch (e) {}

    try {
        obj.PositionX = offsetX;
        obj.PositionY = 0;
        obj.PositionZ = 0;
    } catch (e) {}
}

for (let i = 0; i < items.length; i++) {
    const { width, height, count } = items[i];
    const quantity = Number(count) || 1;

    for (let j = 0; j < quantity; j++) {
        placePanel(Number(width) || 0, Number(height) || 0, currentX);
        currentX += (Number(width) || 0) + gap;
    }
}

alert('Панели созданы по списку.');