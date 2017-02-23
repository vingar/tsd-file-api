
# tsd-file-api

A REST API for upload and download of files, authenticated by JWT.

## Development information

Run the API locally as such: `./uwsgi --ini app-conf.ini --pyargv <config-file.yaml>`.

## Usage as a standalone service

Authentication and authorization is the same as for the storage and retrieval APIs. Different user credentials are required for writing and reading files. After signing up, a TSD admin must verify the user before they can get an access  token. When that is done, a token can be requested. The upload token lasts 24 hours while the download token lasts only for one hour. Files are limited to 100MB (unless requested otherwise).

### Example: uploading files

Current allowed file types are: `'txt', 'pdf', 'png', 'jpg', 'jpeg', 'csv', 'tsv', 'asc'`.

Suppose we are working with a file named `file.ext` and that the API is available at URL `url`.

```bash
curl http://url/upload_signup --request POST -H "Content-Type: application/json" --data '{ "email": "your.email@whatever.com", "pass": "your-password"  }'
curl http://url/upload_token --request POST -H "Content-Type: application/json" --data '{ "email": "your.email@whatever.com", "pass": "your-password"  }'
```

The API caters for both plain-text and PGP encrypted files. Clients can upload plain-text file as follows, using the `multipart/form-data` [MIME type](https://tools.ietf.org/html/rfc1341):

```bash
curl -i --form 'file=@file.ext;filename="file.ext"' -H "Authorization: Bearer $token" -H "Content-Type: multipart/form-data" http://url/upload
```

This curl-based example emulates uploading a file from a web form.

PGP encrypted files are also supported. Clients are recommended to use the `multipart/encrypted` Content-Type header described in [rfc1847](https://tools.ietf.org/html/rfc1847) and elaborated for PGP in [rfc3156](https://tools.ietf.org/html/rfc3156). Doing so will allow the API to initiate processing, such as decryption, on behalf of the client.

```bash
curl -i --form 'file=@file.ext.asc;filename=file.ext.asc' -H 'Content-Type: multipart/encrypted; protocol="application/pgp-encrypted"' http://url/upload
```

### Example: large files and streaming

Large files can be uploaded as binary data. Incoming request data are written to a file byte-for-byte, in order. No data processing is done. All incoming bytes are preserved and written to a file as is. If a file is being streamed, for example, it is the client's responsibilty to construct the binary stream correctly, so that when the bytes are written to the file, data integrity will be preserved.

Cliets should provide a file name in a custom header: `X-Filename: <filename>`. If no filename is provided the current ISO 8601 timestamp will be chosen.

Nginx sets the maximum Content-Length allowed for the stream on a per request basis. If the data stream is smaller than the maximum Content-Length then a file can be streamed using POST:

``` bash
curl -X POST --data-binary @file -H 'Content-Type: application/octet-stream' \
    -H 'X-Filename: filename' http://url/stream
```

If the data stream exceeds maximum Content-Length then data can be sent in consecutive streams, in separate requests. Incoming streams are appended to each other, byte-for-byte. Suppose a large file is split into two files (file1 and file2), clients can send streams to the same file using PATCH:

```bash
curl -X PATCH --data-binary @file1 -H 'Content-Type: application/octet-stream' \
    -H 'X-Filename: filename' http://url/stream

curl -X PATCH --data-binary @file2 -H 'Content-Type: application/octet-stream' \
    -H 'X-Filename: filename' http://url/stream
```

In this case the filename _must_ be provided, otherwise the streams will end up in separate files.

### Getting file metadata

Clients typically want to know which files have been stored and when. This information is available to users who authenticate with upload and/or download tokens.

```bash
curl -i http://url/list -H "Authorization: Bearer $token"
```

The result will show an alphabetical order of files along with the latest time of content modification.

### Example: downloading files

```bash
curl http://url/download_signup --request POST -H "Content-Type: application/json" --data '{ "email": "your.email@whatever.com", "pass": "your-password"  }'
curl http://url/download_token --request POST -H "Content-Type: application/json" --data '{ "saml_data": <saml_data> }'
# downloading a file
curl http://url/download/file.ext --request GET -H "Authorization: Bearer <token>"
```

## Usage in combination with tsd-data API (storage and retrieval APIs).

See [link to docs](LINK!).
