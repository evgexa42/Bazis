let file = 'C:\\Users\\жщшо\\Documents\\Bazis\\#_Фрагменты\\Фрагменты элементов мебели\\Fasad_R.fr3d';
let frag = OpenFurniture(file);
if (frag) {
    let obj = frag.Make();
    obj.Owner = Model.Temp;
    obj.Build();
    obj.ElasticResize({x: 200, y: 600, z: 0});
}