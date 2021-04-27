from ruamel.yaml import YAML
yaml = YAML(typ='safe')
with open('tests.fmf') as tests:
    print(yaml.load(tests)['summary'])
