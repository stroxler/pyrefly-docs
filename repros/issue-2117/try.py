from typing import reveal_type
import pandas as pd

my_df = pd.DataFrame([[1, 2, 3], [4, 5, 6]], columns=pd.Index(["a", "b", "c"]))
reveal_type(my_df)
other_df = my_df[["a", "b"]]
reveal_type(other_df)
other_df2 = my_df[["a", "b"]]
reveal_type(other_df2)
new_df: pd.DataFrame = other_df
reveal_type(new_df)
