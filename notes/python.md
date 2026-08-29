这是关于Python的学习笔记

Python列表（List）是最核心、最常用的内置数据类型之一。它**有序、可变**，且可以存放**任意类型**的混合数据。掌握列表，就掌握了Python数据处理的半壁江山。

以下是全网最清晰、实用的Python列表基本用法全攻略：

### 1. 创建列表（增）
- **空列表**：`my_list = []` 或 `my_list = list()`
- **直接初始化**：`numbers = [1, 2, 3]`
- **混合类型**：`mixed = [1, "Hello", 3.14, True]`
- **范围生成**：`range_list = list(range(5))`  `# [0, 1, 2, 3, 4]`
### 2. 遍历列表（循环）
- **遍历元素**：`for item in nums: print(item)`
- **遍历索引+元素**（最推荐）：
  ```python
  for index, value in enumerate(nums):
      print(f"索引{index}的值是{value}")
  ```
### ⚠️ 新手最容易踩的“深坑”（赋值陷阱）
**错误示范**：`list2 = list1` 
这**不会**复制列表，只是给原列表贴了个新标签。修改`list2`会连带着改掉`list1`。
**正确复制（3种方法）**：
```python
list2 = list1.copy()      # 方法1
list2 = list1[:]          # 方法2（切片）
list2 = list(list1)       # 方法3（构造方法）
```
*(注：如果列表里嵌套了列表，这叫“浅拷贝”；如需完全独立，需用`copy.deepcopy`)*

---


