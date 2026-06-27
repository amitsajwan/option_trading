const db = connect('mongodb://localhost:27017/trading_ai');
const cols = db.getCollectionNames();
print('collections:', cols.join(','));
const candidates = cols.filter(c => c.includes('snapshot'));
for (const c of candidates) {
    const coll = db.getCollection(c);
    const one = coll.findOne({});
    print('---', c, 'count:', coll.countDocuments({}));
    if (one) {
        print('top keys:', Object.keys(one).join(','));
        if (one.snapshot) print('snapshot keys:', Object.keys(one.snapshot).join(','));
    }
}
