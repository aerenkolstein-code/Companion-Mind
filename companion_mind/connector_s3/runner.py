"""Offline-only S3 CLI: emits status and hashes, never credentials or bodies."""
import argparse
import json
from .transport import FixedRequest, MockFixedTransport


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('preflight', 'mock-transaction'))
    args = parser.parse_args(argv)
    if args.command == 'preflight':
        result = {'mode': 'LOCAL_ONLY', 'real_oauth': 0, 'real_api': 0, 'transport': 'MOCK_ONLY'}
    else:
        request = FixedRequest('docs.batchUpdate', 'POST', 'synthetic-doc', '0' * 64, 'synthetic-revision')
        response = MockFixedTransport().dispatch(request)
        result = {'mode': 'LOCAL_ONLY', 'real_oauth': 0, 'real_api': 0,
                  'intent_hash': request.intent_hash, 'transport_status': response['status']}
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
