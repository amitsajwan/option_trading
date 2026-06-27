db.adminCommand('listDatabases').databases.forEach(d => print(d.name));
