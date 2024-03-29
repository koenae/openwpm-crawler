var level = require('level')
const LineByLineReader = require('line-by-line')
const fsp = require('fs').promises

var db = level('./content')

let dir = './hashcontent/'

level('content.ldb', { createIfMissing: false }, function (err, db) {
  if (err instanceof level.errors.OpenError) {
    console.log('failed to open database')
  }

  let lr = new LineByLineReader('hashes')
  lr.on('line', (line) => {
    db.get(line, function (err, content) {
        if (content.includes('cmp')) {
            console.log(line, ' contains cmp function')
            storeContent(line, content)
        } else if (content.includes('tcfapi')) {
            console.log(line, ' contains tcfapi function')
            storeContent(line, content)
        } else if (content.includes('consent')) {
            console.log(line, ' contains consent function')
            storeContent(line, content)
        }
    })
  })
})

async function storeContent(fileName, content) {
    await fsp.appendFile(dir + fileName, content)
}