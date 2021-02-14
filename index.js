var level = require('level')

var db = level('./content')

level('content.ldb', { createIfMissing: false }, function (err, db) {
  if (err instanceof level.errors.OpenError) {
    console.log('failed to open database')
  }

  db.get('b0484bb78a832eefe3549afb313d52399e5b6de182d904da07d5b9da820848ac', function (err, val) {
    console.log('value: ', val)
  })
})