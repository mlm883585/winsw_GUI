import os

from core.config_manager import ConfigManager


class TemplateManager:
    """
    负责扫描并加载 templates/ 目录下的服务模板。
    模板本身是符合 WinSW 规范的 XML，直接复用 ConfigManager 解析。
    """

    def __init__(self, template_dir: str = "templates"):
        self.template_dir = template_dir
        self.config_manager = ConfigManager()

    def list_templates(self):
        """扫描模板目录，返回 [(显示名, 文件路径), ...]，按显示名排序。

        显示名为文件名去扩展名后首字母大写，例如 redis.xml -> 'Redis'。
        """
        templates = []
        try:
            files = [f for f in os.listdir(self.template_dir) if f.endswith(".xml")]
        except FileNotFoundError:
            return templates

        for filename in files:
            stem = os.path.splitext(filename)[0]
            display_name = stem.capitalize()
            path = os.path.join(self.template_dir, filename)
            templates.append((display_name, path))

        return sorted(templates, key=lambda item: item[0].lower())

    def load_template(self, path: str) -> dict:
        """加载指定模板文件，返回配置字典（复用 ConfigManager）。"""
        return self.config_manager.load_from_xml(path)
