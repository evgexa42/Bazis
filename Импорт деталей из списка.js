

//var fileText = system.askReadTextFile().split('\n');

//const lines = ['First line', 'Second line', 'Third line'];
const lines = system.askReadTextFile().split('\n');
//for (let i = 0; i < lines.length; i++) {
  //const [x, y, z] = lines[i].split(' ');
  //console.log(`x = ${x}, y = ${y} z = ${z}`);
//}
var zz = 0;
var xpos = 0;

for (let i = 0; i < lines.length; i++) {
  const [x, y, z] = lines[i].split(' ');

  for (let j = 0; j < z; j++) {
    console.log(`x = ${x}, y = ${y}`);
    zz = zz + 100;

//    pan =  AddHorizPanel (y, x, 0, 0, zz);

	pan1 = AddFrontPanel (xpos, 0, (xpos + Number(y)), Number(x), 0);

	xpos = xpos + Number(y) + 100;

  }

}

//var y = 0
//var pan =  AddHorizPanel (100, 100, 0, 0, y);
